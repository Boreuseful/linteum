"""AgentHost: one assembly point for bus, storage, LLM, tools and sessions (§7.8).

Every front-end used to rebuild this graph by hand (bus + WAL + executor +
tool runtime + agent + checkpointer), which made it easy for two front-ends to
disagree about model configuration, tool policy or recovery.  ``AgentHost`` is
the host-side counterpart of the pure reducer: it owns the impure configuration
and lifecycle concerns so ``bus`` / ``runtime`` stay replayable.

Responsibilities (§4.2 / §7):

* build the storage bundle (SQLite WAL + checkpoints + file artifacts);
* build the bus and its effect subscribers (LLM executor, tool runtime,
  compactor, interrupt controller) exactly once;
* resolve ``agent.profiles`` + ``agent.fallbacks`` into router aliases;
* keep one ``Agent`` per session (in-process single writer) and restore it from
  checkpoint + WAL;
* rebuild pending approvals from the WAL before re-subscribing the reducer, so a
  restart cannot silently drop a human-in-the-loop call (§6.7 / §7.5);
* snapshot the effective agent configuration into ``config_digest``.

It deliberately contains **no wire protocol**: ``linteum_agent.gateway`` sits on
top of it and translates commands/frames.  The host may import ``bus`` /
``storage``; neither imports the host.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import (
    Any,
    Callable,
    Mapping,
    Optional,
    Protocol,
    Sequence,
    runtime_checkable,
)

from ..bus.bus import InMemoryEventBus
from ..bus.event import Event, uuid7
from ..bus.scheduler.cancel import CancelRegistry
from ..config.options import SessionOptions
from ..config.schema import AppConfig, ConfigError
from ..config.service import ConfigService, EffectiveConfig
from ..storage.artifacts import FileArtifactStore
from ..storage.base import SessionInfo, StorageBundle
from ..storage.service import SessionService
from ..storage.sqlite import SqliteStorage
from .agent import Agent, AgentBusy
from .compaction import ContextCompactor
from .context import CompactingContextBuilder, ContextPolicy, PassthroughContextBuilder
from .interrupt import InterruptController
from .llm.adapters import create_adapter
from .llm.executor import LLMExecutor
from .llm.registry import AdapterRegistry
from .llm.router import LlmRouter
from .modes.catalog import load_mode_catalog
from .modes.errors import ModeError, ModeLimit
from .modes.model import (
    ModeCatalog,
    ModeLimits,
    ModeResolution,
    ModeSnapshot,
    ModeSource,
    ModeSummary,
)
from .modes.selection import resolve_mode
from .modes.source import BuiltinModeSource
from .observability.run_logger import RunLogger
from .skills.catalog import build_catalog, filter_catalog, load_catalog
from .skills.model import SkillCatalog, SkillLimits
from .skills.source import (
    PRIORITY_EXTRA,
    PRIORITY_PROJECT,
    PRIORITY_USER,
    SOURCE_EXTRA,
    SOURCE_PROJECT,
    SOURCE_USER,
    DirectorySkillSource,
    SkillSource,
)
from .skills.tools import make_skill_tools
from .state import phases
from .state.agent_state import Budget
from .tools.approval import ApprovalGate
from .tools.registry import ToolRegistry
from .tools.results import InMemoryResultStore
from .tools.runtime import DEFAULT_INLINE_LIMIT, ToolRuntime
from .tools.sandbox import Sandbox
from .tools.workspace import (
    Workspace,
    WorkspaceRegistry,
    WorkspaceToolRegistries,
    make_bash_tool,
    make_file_tools,
)

#: Error code used when a host call references an unknown session.
E_SESSION_NOT_FOUND = "E_SESSION_NOT_FOUND"
#: Error code used when a persisted workspace cannot be reused/opened.
E_SESSION_WORKSPACE = "E_SESSION_WORKSPACE"
#: Error code used when the requested profile does not exist.
E_PROFILE_NOT_FOUND = "E_CONFIG_PROFILE_NOT_FOUND"
#: Error code used when another process owns the session lease.
E_SESSION_LOCKED = "E_SESSION_LOCKED"

#: Metadata keys the host owns; client-supplied values are stripped on create.
_RESERVED_MODE_METADATA = frozenset({
    "mode",
    "mode_digest",
    "mode_source",
    "mode_snapshot",
    "mode_snapshot_inline",
    "mode_tools_count",
    "mode_skills_count",
})


@runtime_checkable
class LeaseManager(Protocol):
    """Structural cross-process lease surface used by :class:`AgentHost`.

    ``gateway.leases.FileLeaseManager`` implements it; keeping it structural
    means ``runtime`` never imports ``gateway`` (the dependency direction stays
    one-way).
    """

    heartbeat_interval: float

    async def acquire(self, session_id: str, *, endpoint: Optional[str] = None): ...

    async def heartbeat(self, session_id: str): ...

    def release(self, session_id: str) -> None: ...


class SessionNotFound(LookupError):
    """Raised by host lookups when a session is not in the catalogue."""

    code = E_SESSION_NOT_FOUND

    def __init__(self, session_id: str) -> None:
        super().__init__(f"session not found: {session_id!r}")
        self.session_id = session_id


class SessionLocked(RuntimeError):
    """Another process holds the session lease (§6.1)."""

    code = E_SESSION_LOCKED

    def __init__(self, session_id: str, owner: Optional[dict] = None) -> None:
        super().__init__(f"session {session_id!r} is owned by another process")
        self.session_id = session_id
        self.owner = dict(owner or {})


def profile_digest(profile: str, llm) -> str:
    """Deterministic digest of a session's resolved agent configuration.

    Secrets are deliberately excluded (rotating a key must not look like a
    configuration change), matching the codebase's ``context.digest`` style.
    """
    payload = {
        "profile": profile,
        "provider": llm.provider,
        "model": llm.model,
        "base_url": llm.resolved_base_url(),
        "openai_mode": llm.resolved_openai_mode(),
        "temperature": llm.temperature,
        "max_tokens": llm.max_tokens,
        "top_p": llm.top_p,
        "reasoning_effort": llm.reasoning_effort,
        "thinking": llm.thinking,
    }
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()


@dataclass
class ModeBinding:
    """Host object tying a resolution to its live registry/context surface.

    Contains callables and a live registry, so it never enters an event
    payload, WAL or ``AgentState``.
    """

    resolution: ModeResolution
    registry: ToolRegistry
    schemas: Callable[[], list[dict]]
    ctx_builder: Any
    context_policy: Optional[ContextPolicy]
    snapshot_ref: Optional[str] = None

    @property
    def digest(self) -> str:
        return self.resolution.digest


@dataclass
class SessionHandle:
    """The host's live view of one session (agent + bound workspace/tools)."""

    session_id: str
    agent: Agent
    workspace: Workspace
    registry: ToolRegistry
    profile: str
    config_digest: str
    warnings: tuple[str, ...] = field(default_factory=tuple)
    #: ``root`` (user session) or ``subagent`` (orchestrator child).
    kind: str = "root"
    #: False for children: the parent session's lease covers the whole tree.
    lease_owned: bool = True
    parent_session_id: Optional[str] = None
    #: JSON-able mode view (root and child) and the root's live binding.
    mode: Optional[ModeSummary] = None
    mode_binding: Optional[ModeBinding] = None


class AgentHost:
    """Assemble and own the event-driven runtime for one process."""

    def __init__(
        self,
        config: EffectiveConfig | AppConfig,
        *,
        config_service: Optional[ConfigService] = None,
        storage: Optional[StorageBundle] = None,
        bus: Optional[InMemoryEventBus] = None,
        adapters: Optional[Mapping[str, Any]] = None,
        result_store=None,
        approvals: Optional[ApprovalGate] = None,
        sandbox: Optional[Sandbox] = None,
        context_compactor: Optional[ContextCompactor] = None,
        interrupt_controller: Optional[InterruptController] = None,
        lease_manager: Optional[LeaseManager] = None,
        endpoint: Optional[str] = None,
        mode_sources: Optional[Sequence[ModeSource]] = None,
    ) -> None:
        self.config_service = config_service
        self._static_config = config.config if isinstance(config, EffectiveConfig) else config
        self.lease_manager = lease_manager
        self.endpoint = endpoint
        cfg = self._current_config()

        # -- storage --------------------------------------------------------
        self.storage = storage if storage is not None else self._default_storage(cfg)
        if not isinstance(self.storage, StorageBundle):  # allow InMemoryStorage
            self.storage = self.storage.bundle

        # -- bus ------------------------------------------------------------
        self.bus = bus if bus is not None else InMemoryEventBus(
            wal=self.storage.wal,
            artifacts=self.storage.artifacts,
            coalesce_window_ms=cfg.gateway.delta.coalesce_window_ms,
        )

        # -- observability sidecar (§48) ------------------------------------
        # The run log is a bus observer, so it must be attached before the
        # dispatch loop starts.  It degrades to stderr if the configured path
        # is unwritable and never raises into the bus.
        self.run_logger = RunLogger.attach(self.bus, cfg.logging)

        # -- effect layers (one subscriber per capability) ------------------
        self.cancel_registry = CancelRegistry()
        self._injected_adapters = dict(adapters or {})
        self._llm_signature: Optional[str] = None
        self.llm_registry = AdapterRegistry()
        self.router = LlmRouter(self.llm_registry)
        self._ensure_llm_routing(cfg)

        self.executor = LLMExecutor(self.bus, self.router, cancel=self.cancel_registry)
        self.results = result_store if result_store is not None else InMemoryResultStore()
        self.sandbox = sandbox if sandbox is not None else Sandbox()
        self.approvals = approvals if approvals is not None else ApprovalGate(self.bus)
        self._workspace_by_session: dict[str, Workspace] = {}
        self._skill_catalogs: dict[str, SkillCatalog] = {}
        self._active_skill_catalogs: dict[str, SkillCatalog] = {}
        #: Extra discovery sources (tests/plugin host).  Builtin is always first.
        self._extra_mode_sources: tuple[ModeSource, ...] = tuple(mode_sources or ())
        self._mode_locks: dict[str, asyncio.Lock] = {}
        #: Subagent sessions may override the root approval policy (§7.3 / S11).
        self._approval_policies: dict[str, str] = {}
        #: Lazily built in :meth:`_ensure_orchestrator` (Task 8); ``None`` keeps
        #: hosts that never enable subagents free of the extra subscriptions.
        self.orchestrator = None
        self.workspaces = WorkspaceRegistry(self._workspace_for)
        self._tool_factory_cfg = cfg
        self.tool_registries = WorkspaceToolRegistries(
            self.workspaces, factory=self._make_tools
        )
        self.tool_runtime = ToolRuntime(
            self.bus,
            ToolRegistry(),                     # fallback; per-session registries win
            self.sandbox,
            self.results,
            self.approvals,
            registry_for=self.tool_registries.for_session,
            artifacts=self.storage.artifacts,
            approval_policy=cfg.agent.tools.approval,
            approval_policy_for=self.approval_policy_for,
            auto_approve=cfg.agent.tools.auto_approve,
        )
        self.compactor = context_compactor or ContextCompactor(
            self.bus, artifacts=self.storage.artifacts
        )
        self.interrupt_controller = interrupt_controller or InterruptController(
            self.bus, self.cancel_registry
        )

        # -- session registry ----------------------------------------------
        self.service = SessionService(self.storage)
        self._handles: dict[str, SessionHandle] = {}
        self._registry_lock = asyncio.Lock()
        self._bus_task: Optional[asyncio.Task] = None
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._closed = False

    # -- configuration ------------------------------------------------------
    def _current_config(self) -> AppConfig:
        if self.config_service is not None:
            return self.config_service.effective(strict=False).config
        return self._static_config

    def current_config(self) -> AppConfig:
        """The effective configuration as of this call (used by the gateway)."""
        return self._current_config()

    @staticmethod
    def _default_storage(cfg: AppConfig) -> StorageBundle:
        path = str(Path(cfg.storage.state_db).expanduser())
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        store = SqliteStorage(
            path,
            keep=cfg.storage.checkpoints.keep,
            every_n_events=cfg.storage.checkpoints.every_n_events,
        )
        artifacts = FileArtifactStore(cfg.storage.artifacts)
        return StorageBundle(
            wal=store, checkpointer=store, artifacts=artifacts, catalog=store
        )

    def _provider_configs(self, cfg: AppConfig) -> dict:
        """First LLMConfig per provider (one adapter per provider, §17.3)."""
        chosen: dict = {}
        candidates = [cfg.agent.llm, *cfg.agent.profiles.values(), *cfg.agent.fallbacks]
        for llm in candidates:
            chosen.setdefault(llm.provider, llm)
        return chosen

    def _ensure_llm_routing(self, cfg: AppConfig) -> None:
        """(Re)build adapters and one alias per profile; keep session pins."""
        signature = "|".join(
            profile_digest(name, cfg.agent.profile(name))
            for name in cfg.agent.profile_names()
        )
        signature += "|fallbacks:" + "|".join(
            profile_digest(f"fallback-{index}", llm)
            for index, llm in enumerate(cfg.agent.fallbacks)
        )
        if signature == self._llm_signature:
            # A provider whose adapter could not be built earlier (missing key
            # at startup) may become usable later; retry it on session open.
            missing = [
                provider for provider in self._provider_configs(cfg)
                if not self.llm_registry.has(provider)
            ]
            if not missing:
                return

        registry = AdapterRegistry()
        seen: set[str] = set()
        for provider, adapter in self._injected_adapters.items():
            registry.register(adapter)
            seen.add(getattr(adapter, "provider", provider))

        for provider, llm in self._provider_configs(cfg).items():
            if provider in seen:
                continue
            try:
                adapter = create_adapter(llm)
            except Exception:  # noqa: BLE001 - no key/SDK: history still works
                continue  # the router filters providers without an adapter
            registry.register(adapter)
            seen.add(provider)

        for name in cfg.agent.profile_names():
            chain = [(item.provider, item.model) for item in cfg.agent.route_chain(name)]
            registry.alias(name, chain)

        router = LlmRouter(registry, breaker=self.router.breaker)
        router._pinned = dict(self.router._pinned)  # preserve I25 session pins
        self.llm_registry = registry
        self.router = router
        if getattr(self, "executor", None) is not None:
            self.executor.router = router
        self._llm_signature = signature

    @staticmethod
    def _context_settings(
        cfg: AppConfig,
        skill_index: str = "",
        extra_blocks: Sequence[str] = (),
        *,
        system_prompt: str = "",
        environment_block: str = "",
    ):
        blocks = tuple(
            block for block in (
                environment_block, skill_index, *extra_blocks,
            ) if block
        )
        ctx = cfg.agent.context
        if ctx.policy == "compacting" and ctx.max_tokens > 0:
            return CompactingContextBuilder(
                system_prompt=system_prompt or None,
                system_blocks=blocks,
            ), ContextPolicy(
                max_tokens=ctx.max_tokens,
                compact_to_tokens=ctx.compact_to_tokens,
                keep_last_messages=ctx.keep_last_messages,
                max_summary_tokens=ctx.max_summary_tokens,
            )
        return PassthroughContextBuilder(
            system_prompt=system_prompt or None,
            system_blocks=blocks,
        ), None

    # -- modes --------------------------------------------------------------
    def _mode_sources(self) -> tuple[ModeSource, ...]:
        return (BuiltinModeSource(), *self._extra_mode_sources)

    async def _build_mode_catalog(self, cfg: AppConfig) -> ModeCatalog:
        limits = ModeLimits(
            max_mode_bytes=cfg.agent.modes.max_mode_bytes,
            max_modes=cfg.agent.modes.max_modes,
        )
        return await load_mode_catalog(self._mode_sources(), limits=limits)

    def _base_tools(self, workspace: Workspace) -> list:
        cfg = self._tool_factory_cfg
        return [
            *make_file_tools(workspace),
            make_bash_tool(workspace, timeout=cfg.agent.tools.bash_timeout),
        ]

    @staticmethod
    def _mode_summary(
        resolution: ModeResolution, *, default: bool,
    ) -> ModeSummary:
        return ModeSummary(
            name=resolution.name,
            description=resolution.description,
            source_id=resolution.source_id,
            builtin=resolution.source_id == "builtin",
            default=default,
            digest=resolution.digest,
        )

    async def list_modes(self) -> list[ModeSummary]:
        """List available modes (builtin + enabled plugins), no prompt text."""
        cfg = self._current_config()
        catalog = await self._build_mode_catalog(cfg)
        default_name = cfg.agent.modes.default
        result: list[ModeSummary] = []
        for name in sorted(catalog.entries):
            entry = catalog.entries[name]
            result.append(ModeSummary(
                name=name,
                description=entry.definition.description,
                source_id=entry.source_id,
                builtin=entry.source_id == "builtin",
                default=name == default_name,
            ))
        return result

    async def mode_catalog(self) -> ModeCatalog:
        """The current catalog (used by Gateway ``mode.list`` diagnostics)."""
        return await self._build_mode_catalog(self._current_config())

    @staticmethod
    def _snapshot_digest(snapshot: ModeSnapshot) -> str:
        blob = json.dumps(
            snapshot.to_dict(), ensure_ascii=False, sort_keys=True,
            separators=(",", ":"),
        )
        return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()

    async def _mode_metadata(
        self,
        session_id: str,
        resolution: ModeResolution,
        cfg: AppConfig,
        metadata: dict,
    ) -> Optional[str]:
        """Write the mode snapshot artifact/inline and fill metadata.

        Returns the artifact ref (or ``None`` for the inline fallback).  The
        artifact write happens before the metadata upsert (I12).
        """
        payload = json.dumps(
            resolution.snapshot.to_dict(), ensure_ascii=False, sort_keys=True,
            separators=(",", ":"),
        )
        encoded = payload.encode("utf-8")
        cap = cfg.agent.modes.max_mode_bytes + 16 * 1024
        if len(encoded) > cap:
            raise ModeLimit(
                f"mode snapshot exceeds {cap} bytes",
                key="max_mode_bytes",
                context={"mode": resolution.name, "bytes": len(encoded)},
            )
        snapshot_ref: Optional[str] = None
        inline: Optional[str] = None
        artifacts = self.storage.artifacts
        if getattr(artifacts, "available", False):
            try:
                artifact = await artifacts.put(
                    encoded, mime="application/json", session_id=session_id,
                )
                snapshot_ref = artifact.ref
            except Exception as exc:  # noqa: BLE001 - inline fallback is valid
                inline = payload
                self._log({
                    "kind": "mode",
                    "level": "warning",
                    "msg": f"mode snapshot artifact failed; using inline: {exc}",
                    "ts": time.time(),
                    "session_id": session_id,
                })
        else:
            inline = payload
        metadata.update({
            "mode": resolution.name,
            "mode_digest": resolution.digest,
            "mode_source": resolution.source_id,
            "mode_snapshot": snapshot_ref,
            "mode_snapshot_inline": inline,
            "mode_tools_count": len(resolution.tool_specs),
            "mode_skills_count": len(resolution.filtered_skills),
        })
        return snapshot_ref

    async def _load_mode_snapshot(self, info: SessionInfo) -> Optional[ModeSnapshot]:
        metadata = dict(info.metadata or {})
        inline = metadata.get("mode_snapshot_inline")
        if inline:
            try:
                return ModeSnapshot.from_dict(json.loads(inline))
            except (TypeError, ValueError):
                return None
        ref = metadata.get("mode_snapshot")
        if not ref:
            return None
        artifacts = self.storage.artifacts
        if not getattr(artifacts, "available", False):
            return None
        key = ref[len("art:"):] if ref.startswith("art:") else ref
        try:
            data = await artifacts.get(key)
            return ModeSnapshot.from_dict(json.loads(data.decode("utf-8")))
        except Exception:  # noqa: BLE001 - missing/corrupt snapshot falls back
            return None

    def _resolution_from_snapshot(
        self,
        snapshot: ModeSnapshot,
        *,
        workspace: Workspace,
        full_skills: SkillCatalog,
        cfg: AppConfig,
        base_tools: Sequence,
    ) -> ModeResolution:
        """Rebuild a resolution from a session snapshot (§9.2)."""
        warnings: list[str] = []
        if snapshot.skills_enabled:
            filtered, skill_warnings = filter_catalog(
                full_skills,
                include=snapshot.skills_include,
                exclude=snapshot.skills_exclude,
            )
            warnings.extend(skill_warnings)
        else:
            filtered = build_catalog((), limits=full_skills.limits)

        loaders = {"load_skill", "read_skill_resource"}
        has_loaders = loaders.issubset(set(snapshot.resolved_tools))
        if filtered.entries and has_loaders:
            skill_specs = list(make_skill_tools(filtered))
        else:
            skill_specs = []
            if filtered.entries:
                warnings.append(
                    "skills loaders are not part of the snapshot; the index "
                    "is suppressed"
                )
                filtered = replace(filtered, index_text="")

        available = {spec.name: spec for spec in (*base_tools, *skill_specs)}
        selected = []
        for name in snapshot.resolved_tools:
            spec = available.get(name)
            if spec is None:
                warnings.append(f"mode tool {name!r} is no longer available")
                continue
            selected.append(spec)

        subagent_tools = (
            frozenset({cfg.agent.subagents.tool_name})
            if snapshot.subagent and cfg.agent.subagents.enabled
            else frozenset()
        )
        digest = self._snapshot_digest(snapshot)
        return ModeResolution(
            name=snapshot.name,
            description=snapshot.description,
            source_id=snapshot.source_id,
            digest=digest,
            snapshot=snapshot,
            system_prompt=snapshot.system_prompt,
            environment_block=snapshot.environment_block,
            tool_specs=tuple(selected),
            subagent_tools=subagent_tools,
            filtered_skills=filtered,
            tool_names=tuple(spec.name for spec in selected),
            skill_names=tuple(entry.name for entry in filtered.entries),
            warnings=tuple(warnings),
        )

    async def set_mode(
        self, session_id: str, mode: str, *, trace_id: Optional[str] = None,
    ) -> SessionHandle:
        """Switch one root session while it is idle (§9.3/§10)."""
        handle = self._handles.get(session_id)
        if handle is None:
            handle = await self.ensure_session(session_id)
        if handle.kind != "root":
            raise ConfigError(
                f"session {session_id!r} is a subagent session",
                key="session_id", code="E_SESSION_SUBAGENT",
            )

        cfg = self._current_config()
        lock = self._mode_locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            handle = self._handles.get(session_id)
            if handle is None:
                raise SessionNotFound(session_id)
            state = handle.agent.state
            if (
                state.phase != phases.IDLE
                or state.pending
                or state.pending_compaction is not None
                or state.interrupt_pending
                or self.approvals.pending_ids(session_id)
            ):
                raise ConfigError(
                    f"session is busy (phase={state.phase!r}); wait for the "
                    "round to finish or interrupt it",
                    key="mode", code="E_MODE_BUSY",
                    hint="retry after the current round completes",
                )

            workspace = (
                self._workspace_by_session.get(session_id)
                or handle.workspace
            )
            full_skills = self._skill_catalogs.get(session_id)
            if full_skills is None:
                full_skills = await self._build_skill_catalog(cfg, workspace)
                self._skill_catalogs[session_id] = full_skills
            catalog = await self._build_mode_catalog(cfg)
            entry = catalog.get(mode)
            if entry is None:
                raise ConfigError(
                    f"mode {mode!r} not found",
                    key="mode", code="E_MODE_NOT_FOUND",
                    hint=(
                        "available modes: "
                        f"{', '.join(sorted(catalog.entries))}"
                    ),
                )
            try:
                resolution = resolve_mode(
                    entry.definition,
                    workspace=workspace,
                    full_skills=full_skills,
                    base_tools=self._base_tools(workspace),
                    subagent_enabled=cfg.agent.subagents.enabled,
                    subagent_tool_name=cfg.agent.subagents.tool_name,
                    limits=ModeLimits(
                        max_mode_bytes=cfg.agent.modes.max_mode_bytes,
                        max_modes=cfg.agent.modes.max_modes,
                    ),
                )
            except ModeError as exc:
                raise ConfigError(
                    str(exc), key=exc.key, code=exc.code, hint=exc.hint,
                ) from exc

            current = handle.mode_binding
            if current is not None and current.digest == resolution.digest:
                self._log({
                    "kind": "mode",
                    "level": "debug",
                    "msg": f"mode refresh is a no-op ({mode})",
                    "ts": time.time(),
                    "session_id": session_id,
                })
                return handle

            registry = ToolRegistry()
            await registry.register_many(resolution.tool_specs)
            builder, policy = self._context_settings(
                cfg,
                system_prompt=resolution.system_prompt,
                environment_block=resolution.environment_block,
                skill_index=resolution.filtered_skills.index_text,
            )
            subagent_tools = resolution.subagent_tools
            schemas = registry.schemas
            subagent_settings = cfg.agent.subagents
            if subagent_tools:
                from .subagents.catalog import SubagentCatalog
                if registry.has(subagent_settings.tool_name):
                    raise ConfigError(
                        f"tool {subagent_settings.tool_name!r} is reserved "
                        "for subagent delegation and cannot be registered",
                        key="agent.subagents.tool_name",
                        code="E_CONFIG_TOOL_RESERVED",
                    )
                registry.reserve(subagent_settings.tool_name)
                subagent_catalog = SubagentCatalog(subagent_settings)

                def schemas(reg=registry, cat=subagent_catalog):
                    return [*reg.schemas(), cat.schema()]

            if subagent_tools:
                self._ensure_orchestrator()

            info = await self.service.get_session(session_id)
            if info is None:
                raise SessionNotFound(session_id)
            metadata = dict(info.metadata or {})
            try:
                snapshot_ref = await self._mode_metadata(
                    session_id, resolution, cfg, metadata,
                )
            except ModeError as exc:
                raise ConfigError(
                    str(exc), key=exc.key, code=exc.code, hint=exc.hint,
                ) from exc
            updated_info = replace(info, metadata=metadata)

            from_digest = current.digest if current is not None else ""
            from_name = (
                current.resolution.name if current is not None
                else (handle.mode.name if handle.mode is not None else "")
            )

            async def commit() -> None:
                await self.storage.catalog.upsert_session(updated_info)
                self.tool_registries.install(session_id, registry)

            try:
                await handle.agent.rebind(
                    ctx_builder=builder,
                    context_policy=policy,
                    schemas=schemas,
                    subagent_tools=subagent_tools,
                    commit=commit,
                )
            except AgentBusy as exc:
                raise ConfigError(
                    str(exc), key="mode", code="E_MODE_BUSY",
                ) from exc
            except ModeError as exc:
                raise ConfigError(
                    str(exc), key=exc.key, code=exc.code, hint=exc.hint,
                ) from exc

            self._active_skill_catalogs[session_id] = (
                resolution.filtered_skills
            )
            mode_summary = self._mode_summary(
                resolution,
                default=(resolution.name == cfg.agent.modes.default),
            )
            binding = ModeBinding(
                resolution=resolution,
                registry=registry,
                schemas=schemas,
                ctx_builder=builder,
                context_policy=policy,
                snapshot_ref=snapshot_ref,
            )
            handle.registry = registry
            handle.mode = mode_summary
            handle.mode_binding = binding
            handle.warnings = resolution.warnings

            payload = {
                "from": from_name,
                "to": resolution.name,
                "from_digest": from_digest,
                "to_digest": resolution.digest,
                "source": resolution.source_id,
                "snapshot_ref": snapshot_ref,
                "refresh": resolution.name == from_name,
            }
            try:
                await self.bus.publish(Event.create(
                    "system.mode.changed",
                    payload,
                    session_id=session_id,
                    trace_id=trace_id,
                ))
            except Exception as exc:  # noqa: BLE001 - observability only
                self._log({
                    "kind": "mode",
                    "level": "warning",
                    "msg": f"system.mode.changed publish failed: {exc}",
                    "ts": time.time(),
                    "session_id": session_id,
                })
            return handle

    def _skill_sources(
        self, cfg: AppConfig, workspace: Workspace
    ) -> tuple[SkillSource, ...]:
        """Build the host-configured source list (the plugin seam is future)."""
        settings = cfg.agent.skills
        if not settings.enabled:
            return ()
        sources: list[SkillSource] = []
        for directory in settings.system_dirs:
            sources.append(DirectorySkillSource(
                Path(directory).expanduser(), SOURCE_USER, PRIORITY_USER,
                missing_ok=True,
            ))
        for directory in settings.extra_dirs:
            sources.append(DirectorySkillSource(
                Path(directory).expanduser(), SOURCE_EXTRA, PRIORITY_EXTRA,
                missing_ok=False,
            ))
        if settings.allow_project:
            sources.append(DirectorySkillSource(
                workspace.root / settings.project_dir,
                SOURCE_PROJECT, PRIORITY_PROJECT, missing_ok=True,
            ))
        return tuple(sources)

    async def _build_skill_catalog(
        self, cfg: AppConfig, workspace: Workspace
    ) -> SkillCatalog:
        settings = cfg.agent.skills
        limits = SkillLimits(
            max_result_bytes=min(
                settings.max_result_bytes, DEFAULT_INLINE_LIMIT - 512
            ),
            max_skill_bytes=settings.max_skill_bytes,
            max_resource_bytes=settings.max_resource_bytes,
            max_resources_per_skill=settings.max_resources_per_skill,
            max_index_bytes=settings.max_index_bytes,
            max_skills=settings.max_skills,
            max_catalog_bytes=settings.max_catalog_bytes,
        )
        return await load_catalog(
            self._skill_sources(cfg, workspace),
            limits=limits,
            max_catalog_bytes=settings.max_catalog_bytes,
            max_index_bytes=settings.max_index_bytes,
            max_skills=settings.max_skills,
        )

    @staticmethod
    def _budget(cfg: AppConfig) -> Budget:
        budget = cfg.agent.budget
        return Budget(
            tokens=budget.tokens,
            usd=budget.usd,
            wall_ms=budget.wall_ms,
            rounds=budget.rounds,
            started_ms=time.time(),
        )

    def _make_tools(self, workspace: Workspace) -> list:
        cfg = self._tool_factory_cfg
        specs = [
            *make_file_tools(workspace),
            make_bash_tool(workspace, timeout=cfg.agent.tools.bash_timeout),
        ]
        catalog = (
            self._active_skill_catalogs.get(workspace.session_id)
            or self._skill_catalogs.get(workspace.session_id)
        )
        if catalog:
            specs.extend(make_skill_tools(catalog))
        return specs

    def _workspace_for(self, session_id: str) -> Workspace:
        workspace = self._workspace_by_session.get(session_id)
        if workspace is None:
            raise SessionNotFound(session_id)
        return workspace

    def _log(self, record: dict) -> None:
        """Best-effort structured record (custom buses may not expose emit_log)."""
        emit = getattr(self.bus, "emit_log", None)
        if emit is not None:
            emit(record)

    # -- lifecycle ----------------------------------------------------------
    async def start(self) -> "AgentHost":
        """Start the bus dispatch loop and the lease heartbeat (idempotent)."""
        if self._bus_task is None:
            self._bus_task = asyncio.create_task(self.bus.run())
        if self.lease_manager is not None and self._heartbeat_task is None:
            self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        return self

    async def _heartbeat_loop(self) -> None:
        """Renew every live session lease until shutdown."""
        interval = float(getattr(self.lease_manager, "heartbeat_interval", 10.0))
        while True:
            await asyncio.sleep(max(1.0, interval))
            for session_id, handle in list(self._handles.items()):
                if not handle.lease_owned:
                    continue  # a child is covered by the parent's lease
                try:
                    lease = await self.lease_manager.heartbeat(session_id)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - a lease blip must not kill the host
                    self._log({
                        "kind": "lease_heartbeat_error",
                        "level": "warning",
                        "msg": f"session {session_id}: {type(exc).__name__}: {exc}",
                        "ts": time.time(),
                        "session_id": session_id,
                    })
                    continue
                if lease is None:
                    # Someone else took over (stale takeover) or the record
                    # vanished; stop reducing immediately (§6.1 single writer).
                    self._log({
                        "kind": "lease_lost",
                        "level": "warning",
                        "msg": f"session {session_id}: lease is no longer owned by this process",
                        "ts": time.time(),
                        "session_id": session_id,
                    })
                    self.close_session(session_id)

    async def close(self) -> None:
        """Unsubscribe every agent, stop the bus and close the storage."""
        if self._closed:
            return
        self._closed = True
        heartbeat, self._heartbeat_task = self._heartbeat_task, None
        if heartbeat is not None:
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat
        if self.orchestrator is not None:
            await self.orchestrator.close()
        for session_id in list(self._handles):
            self.close_session(session_id)
        self.bus.stop()
        await self.bus.close()
        task, self._bus_task = self._bus_task, None
        if task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self.run_logger.close()          # flush deltas after dispatch has drained
        if self.storage.wal is not self.bus.wal:
            with contextlib.suppress(Exception):
                await self.storage.wal.close()
        close = getattr(self.results, "close", None)
        if close is not None:
            result = close()
            if hasattr(result, "__await__"):
                with contextlib.suppress(Exception):
                    await result
        self.compactor.close()

    async def __aenter__(self) -> "AgentHost":
        return await self.start()

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    # -- session lifecycle --------------------------------------------------
    async def open_session(
        self, options: SessionOptions, *, profile: Optional[str] = None
    ) -> SessionHandle:
        """Open (or resume) one session and return its live handle.

        ``profile`` is the *explicit* choice from ``session.create``; ``None``
        means "use the persisted profile, else ``agent.default_profile``".
        Creating a session snapshots the current effective agent configuration.
        """
        cfg = self._current_config()
        self._ensure_llm_routing(cfg)
        self._tool_factory_cfg = cfg
        self.tool_runtime.approval_policy = cfg.agent.tools.approval
        self.tool_runtime.auto_approve = frozenset(cfg.agent.tools.auto_approve)
        session_id = options.session_id or uuid7()

        async with self._registry_lock:
            handle = self._handles.get(session_id)
            if handle is not None:
                if handle.kind == "subagent":
                    raise ConfigError(
                        f"session {session_id!r} is a subagent session; "
                        "it is driven by its parent's orchestrator",
                        key="session_id", code="E_SESSION_SUBAGENT",
                    )
                return handle

            info = await self.service.get_session(session_id)
            if info is not None and info.kind == "subagent":
                raise ConfigError(
                    f"session {session_id!r} is a subagent session; "
                    "it is driven by its parent's orchestrator",
                    key="session_id", code="E_SESSION_SUBAGENT",
                )
            if info is None:
                chosen = profile or options.profile or cfg.agent.default_profile
                try:
                    llm = cfg.agent.profile(chosen)
                except ConfigError as exc:
                    raise ConfigError(
                        str(exc), key="profile", code=E_PROFILE_NOT_FOUND,
                        hint=exc.hint,
                    ) from exc
                digest = profile_digest(chosen, llm)
                user_metadata = {
                    key: value
                    for key, value in dict(options.metadata).items()
                    if key not in _RESERVED_MODE_METADATA
                }
                metadata = {
                    **user_metadata,
                    "profile": chosen,
                    "config_digest": digest,
                }
                info = await self.service.create_session(
                    session_id,
                    title=options.title,
                    workspace=str(options.workspace),
                    metadata=metadata,
                )
                return await self._activate(
                    info, chosen, cfg, digest, (), mode=options.mode,
                    created_here=True,
                )

            # -- resume: workspace and profile are session-scoped facts -----
            warnings: list[str] = []
            persisted = info.workspace
            if not persisted:
                raise ConfigError(
                    f"session {session_id!r} has no workspace",
                    key="workspace", code=E_SESSION_WORKSPACE,
                )
            if Path(persisted) != Path(options.workspace):
                warnings.append(
                    f"ignoring workspace {str(options.workspace)!r}; "
                    f"session is bound to {persisted!r}"
                )
            chosen = profile or info.metadata.get("profile") or options.profile
            try:
                llm = cfg.agent.profile(chosen)
            except ConfigError as exc:
                raise ConfigError(
                    str(exc), key="profile", code=E_PROFILE_NOT_FOUND,
                    hint=exc.hint,
                ) from exc
            digest = profile_digest(chosen, llm)
            stored_digest = info.metadata.get("config_digest")
            if stored_digest and stored_digest != digest:
                warnings.append(
                    "agent configuration changed since the session was created "
                    f"({stored_digest} -> {digest}); the provider/model pin is kept"
                )
            if info.metadata.get("profile") != chosen:
                info = replace(
                    info, metadata={**info.metadata, "profile": chosen}
                )
                await self.storage.catalog.upsert_session(info)
            persisted_mode = info.metadata.get("mode")
            if (
                options.mode is not None
                and persisted_mode is not None
                and options.mode != persisted_mode
            ):
                raise ConfigError(
                    f"session mode is {persisted_mode!r}; open the session, "
                    "then call session.mode.set",
                    key="mode",
                    code="E_MODE_IMMUTABLE",
                )
            return await self._activate(
                info, chosen, cfg, digest, tuple(warnings), mode=options.mode,
            )

    async def ensure_session(
        self, session_id: str, *, profile: Optional[str] = None
    ) -> SessionHandle:
        """Open an existing session from its persisted workspace."""
        handle = self._handles.get(session_id)
        if handle is not None:
            if handle.kind == "subagent":
                raise ConfigError(
                    f"session {session_id!r} is a subagent session; "
                    "it is driven by its parent's orchestrator",
                    key="session_id", code="E_SESSION_SUBAGENT",
                )
            return handle
        info = await self.service.get_session(session_id)
        if info is None:
            raise SessionNotFound(session_id)
        if info.kind == "subagent":
            raise ConfigError(
                f"session {session_id!r} is a subagent session; "
                "it is driven by its parent's orchestrator",
                key="session_id", code="E_SESSION_SUBAGENT",
            )
        if not info.workspace:
            raise ConfigError(
                f"session {session_id!r} has no workspace",
                key="workspace", code=E_SESSION_WORKSPACE,
            )
        options = SessionOptions(
            session_id=session_id,
            workspace=info.workspace,
            profile=info.metadata.get("profile") or profile or "default",
            title=info.title,
            metadata=info.metadata,
        )
        return await self.open_session(options, profile=profile)

    async def _acquire_lease(self, session_id: str):
        """Take the cross-process writer lease (or raise :class:`SessionLocked`)."""
        if self.lease_manager is None:
            return None
        try:
            return await self.lease_manager.acquire(session_id, endpoint=self.endpoint)
        except Exception as exc:  # noqa: BLE001 - structural lease errors
            owner = getattr(exc, "lease", None)
            if owner is not None:
                raise SessionLocked(
                    session_id,
                    owner.to_dict() if hasattr(owner, "to_dict") else None,
                ) from exc
            raise

    def _release_lease(self, session_id: str) -> None:
        if self.lease_manager is None:
            return
        with contextlib.suppress(Exception):
            self.lease_manager.release(session_id)

    async def _activate(
        self,
        info: SessionInfo,
        profile: str,
        cfg: AppConfig,
        digest: str,
        warnings: tuple[str, ...],
        *,
        mode: Optional[str] = None,
        created_here: bool = False,
    ) -> SessionHandle:
        if info.kind == "subagent":
            raise ConfigError(
                f"session {info.session_id!r} is a subagent session",
                key="session_id", code="E_SESSION_SUBAGENT",
            )
        # ★ One live reducer per session, process-wide and machine-wide (§6.1).
        await self._acquire_lease(info.session_id)
        try:
            # Only the lease owner may mutate the child catalogue; doing this
            # before the lease could mark another process's live children as
            # interrupted.
            await self.reconcile_children(info.session_id)
            workspace = Workspace(root=info.workspace, session_id=info.session_id)
            self._workspace_by_session[info.session_id] = workspace
            catalog = await self._build_skill_catalog(cfg, workspace)
            self._skill_catalogs[info.session_id] = catalog

            mode_catalog = await self._build_mode_catalog(cfg)
            metadata = dict(info.metadata or {})
            if created_here:
                for key in _RESERVED_MODE_METADATA:
                    metadata.pop(key, None)
                persisted_mode = None
                snapshot = None
            else:
                persisted_mode = metadata.get("mode")
                snapshot = await self._load_mode_snapshot(info)
            if (
                mode is not None
                and persisted_mode is not None
                and mode != persisted_mode
            ):
                raise ConfigError(
                    f"session mode is {persisted_mode!r}; open the session, "
                    "then call session.mode.set",
                    key="mode", code="E_MODE_IMMUTABLE",
                )
            mode_name = mode or persisted_mode or cfg.agent.modes.default
            if mode is None and persisted_mode is None:
                warnings = (
                    *warnings,
                    f"legacy session: mode defaulted to {mode_name!r}",
                )
            if snapshot is not None:
                resolution = self._resolution_from_snapshot(
                    snapshot,
                    workspace=workspace,
                    full_skills=catalog,
                    cfg=cfg,
                    base_tools=self._base_tools(workspace),
                )
            else:
                entry = mode_catalog.get(mode_name)
                if entry is None:
                    invalid = next(
                        (issue for issue in mode_catalog.invalid
                         if issue.name == mode_name),
                        None,
                    )
                    detail = (
                        f" ({invalid.code}: {invalid.error})"
                        if invalid is not None else ""
                    )
                    raise ConfigError(
                        f"mode {mode_name!r} not found{detail}",
                        key="mode", code="E_MODE_NOT_FOUND",
                        hint=(
                            "available modes: "
                            f"{', '.join(sorted(mode_catalog.entries))}"
                        ),
                    )
                resolution = resolve_mode(
                    entry.definition,
                    workspace=workspace,
                    full_skills=catalog,
                    base_tools=self._base_tools(workspace),
                    subagent_enabled=cfg.agent.subagents.enabled,
                    subagent_tool_name=cfg.agent.subagents.tool_name,
                    limits=ModeLimits(
                        max_mode_bytes=cfg.agent.modes.max_mode_bytes,
                        max_modes=cfg.agent.modes.max_modes,
                    ),
                )
                stored_mode_digest = metadata.get("mode_digest")
                if (
                    stored_mode_digest
                    and stored_mode_digest != resolution.digest
                ):
                    warnings = (
                        *warnings,
                        "mode changed since the session was created; the "
                        "current definition is used",
                    )

            warnings = (*warnings, *catalog.warnings, *resolution.warnings)
            for warning in catalog.warnings:
                self._log({
                    "kind": "skills_catalog",
                    "level": "warning",
                    "msg": warning,
                    "ts": time.time(),
                    "session_id": info.session_id,
                })

            registry = ToolRegistry()
            await registry.register_many(resolution.tool_specs)
            self.tool_registries.install(info.session_id, registry)
            self._active_skill_catalogs[info.session_id] = (
                resolution.filtered_skills
            )
            builder, policy = self._context_settings(
                cfg,
                system_prompt=resolution.system_prompt,
                environment_block=resolution.environment_block,
                skill_index=resolution.filtered_skills.index_text,
            )

            current_digest = catalog.digest if catalog.entries else ""
            stored_digest = metadata.get("skills_digest")
            if stored_digest is not None and stored_digest != current_digest:
                warnings = (*warnings,
                            "skills changed since the session was created "
                            f"({str(stored_digest)[:12] or 'none'} -> "
                            f"{current_digest[:12] or 'none'}); the new snapshot is used")
            metadata["skills_digest"] = current_digest
            metadata["skills_count"] = len(catalog)
            snapshot_ref = await self._mode_metadata(
                info.session_id, resolution, cfg, metadata,
            )
            info = replace(info, metadata=metadata)
            await self.storage.catalog.upsert_session(info)

            budget = self._budget(cfg)
            subagent_tools = resolution.subagent_tools
            schemas = registry.schemas
            subagent_settings = cfg.agent.subagents
            if subagent_tools:
                from .subagents.catalog import SubagentCatalog
                if registry.has(subagent_settings.tool_name):
                    raise ConfigError(
                        f"tool {subagent_settings.tool_name!r} is reserved for "
                        "subagent delegation and cannot be registered",
                        key="agent.subagents.tool_name", code="E_CONFIG_TOOL_RESERVED",
                    )
                registry.reserve(subagent_settings.tool_name)
                subagent_catalog = SubagentCatalog(subagent_settings)

                def schemas(reg=registry, cat=subagent_catalog):
                    return [*reg.schemas(), cat.schema()]

            kwargs = {
                "model": profile,
                "schemas": schemas,
                "budget": budget,
                "ctx_builder": builder,
                "context_policy": policy,
                "subagent_tools": subagent_tools,
            }

            # Restore approvals before closing/reducing the half-round: the calls
            # the gate still owns must be excluded from the "interrupted" sweep.
            events = await self.bus.replay(session_id=info.session_id)
            self.approvals.restore(info.session_id, events)
            pending = self.approvals.pending_ids(info.session_id)

            checkpoint = self.storage.checkpointer.latest(info.session_id)
            if checkpoint is not None or events:
                agent = await Agent.restore(
                    self.bus,
                    self.storage.checkpointer,
                    session_id=info.session_id,
                    pending_approvals=pending,
                    **kwargs,
                )
                if agent.state.config_digest and agent.state.config_digest != digest:
                    warnings = (*warnings,
                                "checkpoint config_digest differs from the current configuration")
                elif not agent.state.config_digest:
                    agent.state.config_digest = digest
            else:
                agent = Agent(
                    self.bus,
                    session_id=info.session_id,
                    checkpointer=self.storage.checkpointer,
                    auto_subscribe=False,
                    **kwargs,
                )
                agent.state.config_digest = digest
                agent.subscribe()

            mode_summary = self._mode_summary(
                resolution, default=(resolution.name == cfg.agent.modes.default),
            )
            binding = ModeBinding(
                resolution=resolution,
                registry=registry,
                schemas=schemas,
                ctx_builder=builder,
                context_policy=policy,
                snapshot_ref=snapshot_ref,
            )
            handle = SessionHandle(
                session_id=info.session_id,
                agent=agent,
                workspace=workspace,
                registry=registry,
                profile=profile,
                config_digest=digest,
                warnings=warnings,
                mode=mode_summary,
                mode_binding=binding,
            )
            self._handles[info.session_id] = handle
            if subagent_settings.enabled:
                self._ensure_orchestrator()
            return handle
        except ModeError as exc:
            self._rollback_activation(info.session_id)
            if created_here:
                await self._delete_failed_create(info.session_id)
            raise ConfigError(
                str(exc), key=exc.key, code=exc.code, hint=exc.hint,
            ) from exc
        except BaseException:
            self._rollback_activation(info.session_id)
            if created_here:
                await self._delete_failed_create(info.session_id)
            raise

    async def _delete_failed_create(self, session_id: str) -> None:
        """Remove a row created by a failed activation (§9.1)."""
        try:
            info = await self.service.get_session(session_id)
            if info is None:
                return
            if info.last_seq == 0 and self.storage.checkpointer.latest(
                session_id
            ) is None:
                await self.service.delete_session(session_id)
        except Exception as exc:  # noqa: BLE001 - best effort cleanup
            self._log({
                "kind": "mode",
                "level": "warning",
                "msg": f"failed to delete half-created session: {exc}",
                "ts": time.time(),
                "session_id": session_id,
            })

    def _rollback_activation(self, session_id: str) -> None:
        self._release_lease(session_id)
        self._workspace_by_session.pop(session_id, None)
        self._skill_catalogs.pop(session_id, None)
        self._active_skill_catalogs.pop(session_id, None)
        self._mode_locks.pop(session_id, None)
        self.workspaces.drop(session_id)
        self.tool_registries.drop(session_id)

    def session(self, session_id: str) -> Optional[SessionHandle]:
        return self._handles.get(session_id)

    def agents(self) -> dict:
        return {session_id: handle.agent for session_id, handle in self._handles.items()}

    def close_session(self, session_id: str) -> None:
        """Detach a session's reducer; durable history stays in the WAL.

        Closing a root cancels its orchestrator work and closes every child
        handle first; children never release a lease of their own (§8.5).
        """
        if self.orchestrator is not None:
            self.orchestrator.cancel_parent(session_id)
        handle = self._handles.pop(session_id, None)
        if handle is not None:
            handle.agent.close()
        self._workspace_by_session.pop(session_id, None)
        self._skill_catalogs.pop(session_id, None)
        self._active_skill_catalogs.pop(session_id, None)
        self._mode_locks.pop(session_id, None)
        self.workspaces.drop(session_id)   # a reused id must re-resolve its root
        self.tool_registries.drop(session_id)
        self._approval_policies.pop(session_id, None)
        self.approvals.cancel(session_id=session_id)
        if handle is None or handle.lease_owned:
            self._release_lease(session_id)  # another process may now take over
        if handle is not None and handle.kind == "root":
            children = [
                child_id for child_id, child in list(self._handles.items())
                if child.parent_session_id == session_id
            ]
            for child_id in children:
                self.close_session(child_id)

    async def delete_session(self, session_id: str) -> None:
        """Close and erase a session and its child catalogue/checkpoint rows."""
        self.close_session(session_id)
        for child in await self.service.list_children(session_id):
            await self.delete_session(child.session_id)
        await self.service.delete_session(session_id)

    async def reconcile_children(self, parent_session_id: str) -> None:
        """Mark orphaned children interrupted without calling a provider (§7.8).

        Recovery must never re-run a delegated task or synthesise a terminal:
        the parent's half-round closer already pairs the calls as interrupted.
        """
        for child in await self.service.list_children(parent_session_id):
            status = (child.metadata or {}).get("subagent_status")
            if status not in ("spawned", "running"):
                continue
            metadata = {**child.metadata, "subagent_status": "interrupted"}
            await self.storage.catalog.upsert_session(
                replace(child, metadata=metadata)
            )
            self.close_session(child.session_id)

    # -- subagent assembly --------------------------------------------------
    def _ensure_orchestrator(self):
        """Build the effect layer once, without a host<->subagents import cycle.

        A config reload must affect new sessions (§9.3): refresh the settings
        and catalog in place.  Running children keep the ``SubagentPolicy``
        they captured at spawn time.
        """
        settings = self._current_config().agent.subagents
        if self.orchestrator is None:
            from .subagents.orchestrator import SubagentOrchestrator
            self.orchestrator = SubagentOrchestrator(
                self.bus, self, settings, artifacts=self.storage.artifacts,
            )
        elif self.orchestrator.settings != settings:
            from .subagents.catalog import SubagentCatalog
            self.orchestrator.settings = settings
            self.orchestrator.catalog = SubagentCatalog(settings)
        return self.orchestrator

    def approval_policy_for(self, session_id: str) -> Optional[str]:
        """Per-session approval policy consumed by :class:`ToolRuntime`.

        ``None`` means "use the host-wide policy"; a child's ``inherit`` is
        resolved against the current root policy on every call.
        """
        policy = self._approval_policies.get(session_id)
        if policy == "inherit":
            return self._current_config().agent.tools.approval
        if policy in ("allow", "deny"):
            return policy
        return None

    def subagent_tool_specs(self, parent_session_id: str) -> dict[str, str]:
        """``name -> side_effect`` for the parent's live, non-spawn tools."""
        handle = self._handles.get(parent_session_id)
        registry = (
            handle.registry if handle is not None
            else self.tool_registries.built(parent_session_id)
        )
        if registry is None:
            return {}
        reserved = self._current_config().agent.subagents.tool_name
        return {
            name: registry.spec(name).side_effect
            for name in registry.names()
            if name != reserved
        }

    async def open_subagent_session(
        self,
        *,
        parent_session_id: str,
        call_id: str,
        task_title: str,
        trace_id: str,
        workspace_root: str,
        tools: Sequence[str],
        model_profile: str,
        system_blocks: Sequence[str],
        budget: Budget,
        read_only: bool,
        approval_policy: str,
        metadata: Mapping[str, Any],
    ) -> SessionHandle:
        """Assemble one isolated child session (design §7.4).

        The child shares the parent workspace root (optionally read-only), gets
        its own ``session_id`` / state / WAL / checkpoint / registry, and does
        **not** acquire a lease or receive the reserved spawn schema.
        """
        from .subagents.errors import (
            E_SUBAGENT_CANCELLED,
            E_SUBAGENT_DEPTH,
            E_SUBAGENT_SPAWN,
            SubagentError,
        )

        parent = self._handles.get(parent_session_id)
        if parent is None:
            raise SubagentError(
                E_SUBAGENT_SPAWN, "parent session is not open",
                context={"parent_session_id": parent_session_id,
                         "call_id": call_id},
            )
        if parent.kind != "root":
            raise SubagentError(
                E_SUBAGENT_DEPTH, "subagents cannot spawn subagents",
                context={"parent_session_id": parent_session_id,
                         "call_id": call_id},
            )
        if self._closed:
            raise SubagentError(
                E_SUBAGENT_SPAWN, "host is closed",
                context={"parent_session_id": parent_session_id,
                         "call_id": call_id},
            )

        cfg = self._current_config()
        self._ensure_llm_routing(cfg)
        self._tool_factory_cfg = cfg
        spawn_name = cfg.agent.subagents.tool_name
        child_id = uuid7()
        workspace = Workspace(
            root=workspace_root, session_id=child_id, read_only=read_only,
        )
        parent_catalog = self._skill_catalogs.get(parent_session_id)
        parent_active = self._active_skill_catalogs.get(parent_session_id)
        inherit_skills = cfg.agent.subagents.inherit_skills
        skill_index = (
            parent_active.index_text
            if inherit_skills and parent_active is not None else ""
        )

        agent = None
        try:
            self._workspace_by_session[child_id] = workspace
            if inherit_skills and parent_catalog is not None:
                self._skill_catalogs[child_id] = parent_catalog
            if inherit_skills and parent_active is not None:
                self._active_skill_catalogs[child_id] = parent_active

            available = {spec.name: spec for spec in self._make_tools(workspace)}
            selected = []
            for name in tools:
                if name == spawn_name:
                    raise SubagentError(
                        E_SUBAGENT_DEPTH,
                        f"{name!r} would exceed "
                        f"max_depth={cfg.agent.subagents.max_depth}",
                        context={"parent_session_id": parent_session_id,
                                 "call_id": call_id},
                    )
                spec = available.get(name)
                if spec is None:
                    raise SubagentError(
                        E_SUBAGENT_SPAWN,
                        f"tool {name!r} is not available for a child session",
                        context={"parent_session_id": parent_session_id,
                                 "call_id": call_id,
                                 "requested_tools": list(tools),
                                 "available_tools": sorted(available)},
                    )
                selected.append(spec)

            registry = ToolRegistry()
            await registry.register_many(selected)
            self.tool_registries.install(child_id, registry)

            builder, policy = self._context_settings(
                cfg, skill_index=skill_index, extra_blocks=system_blocks,
            )
            child_budget = Budget(
                tokens=budget.tokens,
                usd=budget.usd,
                wall_ms=budget.wall_ms,
                rounds=budget.rounds,
                started_ms=time.time(),
            )
            agent = Agent(
                self.bus,
                session_id=child_id,
                model=model_profile,
                schemas=registry.schemas,
                checkpointer=self.storage.checkpointer,
                budget=child_budget,
                ctx_builder=builder,
                context_policy=policy,
                subagent_tools=frozenset(),   # max_depth=1 at the capability layer
                auto_subscribe=False,
            )
            agent.state.trace_id = trace_id
            agent.state.config_digest = profile_digest(
                model_profile, cfg.agent.profile(model_profile)
            )
            agent.subscribe()

            # The parent may have closed while we awaited registry/storage
            # work; never publish a child for a session this host no longer
            # owns.
            if self._closed or parent_session_id not in self._handles:
                raise SubagentError(
                    E_SUBAGENT_CANCELLED,
                    "parent session closed while spawning the child",
                    context={"parent_session_id": parent_session_id,
                             "call_id": call_id},
                )

            now = time.time()
            row_metadata = {
                "parent_session_id": parent_session_id,
                "call_id": call_id,
                "subagent_type": "spawn_subagent",
                "subagent_status": "spawned",
                "model_profile": model_profile,
                "tools": list(tools),
                "read_only": bool(read_only),
                **dict(metadata),
            }
            info = SessionInfo(
                session_id=child_id,
                created_at=now,
                updated_at=now,
                title=(task_title or "subagent")[:120],
                workspace=str(workspace_root),
                metadata=row_metadata,
                parent_session_id=parent_session_id,
                kind="subagent",
            )
            await self.storage.catalog.upsert_session(info)
        except BaseException:
            if agent is not None:
                agent.close()
            self._workspace_by_session.pop(child_id, None)
            self._skill_catalogs.pop(child_id, None)
            self._active_skill_catalogs.pop(child_id, None)
            self.tool_registries.drop(child_id)
            raise

        self._approval_policies[child_id] = approval_policy
        handle = SessionHandle(
            session_id=child_id,
            agent=agent,
            workspace=workspace,
            registry=registry,
            profile=model_profile,
            config_digest=agent.state.config_digest,
            parent_session_id=parent_session_id,
            kind="subagent",
            lease_owned=False,
            mode=parent.mode,
            mode_binding=None,
        )
        self._handles[child_id] = handle
        return handle

    # -- reads --------------------------------------------------------------
    async def list_sessions(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        workspace: Optional[str] = None,
        include_subagents: bool = False,
        parent_session_id: Optional[str] = None,
    ) -> list[SessionInfo]:
        return await self.service.list_sessions(
            limit=limit,
            offset=offset,
            workspace=workspace,
            include_subagents=include_subagents,
            parent_session_id=parent_session_id,
        )

    async def list_children(
        self, parent_session_id: str, *, limit: int = 50, offset: int = 0,
    ) -> list[SessionInfo]:
        return await self.service.list_children(
            parent_session_id, limit=limit, offset=offset,
        )

    async def get_session_info(self, session_id: str) -> Optional[SessionInfo]:
        return await self.service.get_session(session_id)

    async def history(self, session_id: str, *, include_inflight: bool = True) -> list[dict]:
        return await self.service.history(
            session_id, include_inflight=include_inflight
        )

    async def replay(self, session_id: str, *, after_seq: int = 0) -> list:
        """Committed events after ``after_seq`` (the gateway's resume cursor)."""
        return await self.bus.replay(session_id=session_id, after_seq=after_seq)

    async def last_seq(self, session_id: str) -> int:
        return await self.storage.wal.last_seq(session_id)


__all__ = [
    "AgentHost",
    "LeaseManager",
    "SessionHandle",
    "SessionLocked",
    "SessionNotFound",
    "profile_digest",
    "E_PROFILE_NOT_FOUND",
    "E_SESSION_LOCKED",
    "E_SESSION_NOT_FOUND",
    "E_SESSION_WORKSPACE",
]
