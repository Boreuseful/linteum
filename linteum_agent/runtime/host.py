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
from typing import Any, Mapping, Optional, Protocol, Sequence, runtime_checkable

from ..bus.bus import InMemoryEventBus
from ..bus.event import uuid7
from ..bus.scheduler.cancel import CancelRegistry
from ..config.options import SessionOptions
from ..config.schema import AppConfig, ConfigError
from ..config.service import ConfigService, EffectiveConfig
from ..storage.artifacts import FileArtifactStore
from ..storage.base import SessionInfo, StorageBundle
from ..storage.service import SessionService
from ..storage.sqlite import SqliteStorage
from .agent import Agent
from .compaction import ContextCompactor
from .context import CompactingContextBuilder, ContextPolicy, PassthroughContextBuilder
from .interrupt import InterruptController
from .llm.adapters import create_adapter
from .llm.executor import LLMExecutor
from .llm.registry import AdapterRegistry
from .llm.router import LlmRouter
from .observability.run_logger import RunLogger
from .skills.catalog import load_catalog
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
    ):
        blocks = (*((skill_index,) if skill_index else ()), *tuple(extra_blocks))
        ctx = cfg.agent.context
        if ctx.policy == "compacting" and ctx.max_tokens > 0:
            return CompactingContextBuilder(system_blocks=blocks), ContextPolicy(
                max_tokens=ctx.max_tokens,
                compact_to_tokens=ctx.compact_to_tokens,
                keep_last_messages=ctx.keep_last_messages,
                max_summary_tokens=ctx.max_summary_tokens,
            )
        return PassthroughContextBuilder(system_blocks=blocks), None

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
        catalog = self._skill_catalogs.get(workspace.session_id)
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
                metadata = {
                    **dict(options.metadata),
                    "profile": chosen,
                    "config_digest": digest,
                }
                info = await self.service.create_session(
                    session_id,
                    title=options.title,
                    workspace=str(options.workspace),
                    metadata=metadata,
                )
                return await self._activate(info, chosen, cfg, digest, ())

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
            return await self._activate(info, chosen, cfg, digest, tuple(warnings))

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
            registry = await self.tool_registries.for_session(info.session_id)

            builder, policy = self._context_settings(cfg, catalog.index_text)
            warnings = (*warnings, *catalog.warnings)
            for warning in catalog.warnings:
                self._log({
                    "kind": "skills_catalog",
                    "level": "warning",
                    "msg": warning,
                    "ts": time.time(),
                    "session_id": info.session_id,
                })
            current_digest = catalog.digest if catalog.entries else ""
            metadata = dict(info.metadata or {})
            stored_digest = metadata.get("skills_digest")
            if stored_digest is not None and stored_digest != current_digest:
                warnings = (*warnings,
                            "skills changed since the session was created "
                            f"({str(stored_digest)[:12] or 'none'} -> "
                            f"{current_digest[:12] or 'none'}); the new snapshot is used")
            if (metadata.get("skills_digest") != current_digest
                    or metadata.get("skills_count") != len(catalog)):
                metadata["skills_digest"] = current_digest
                metadata["skills_count"] = len(catalog)
                info = replace(info, metadata=metadata)
                await self.storage.catalog.upsert_session(info)
            budget = self._budget(cfg)
            subagent_tools: frozenset[str] = frozenset()
            schemas = registry.schemas
            subagent_settings = cfg.agent.subagents
            if subagent_settings.enabled:
                from .subagents.catalog import SubagentCatalog
                if registry.has(subagent_settings.tool_name):
                    raise ConfigError(
                        f"tool {subagent_settings.tool_name!r} is reserved for "
                        "subagent delegation and cannot be registered",
                        key="agent.subagents.tool_name", code="E_CONFIG_TOOL_RESERVED",
                    )
                registry.reserve(subagent_settings.tool_name)
                subagent_catalog = SubagentCatalog(subagent_settings)
                subagent_tools = frozenset({subagent_settings.tool_name})

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

            handle = SessionHandle(
                session_id=info.session_id,
                agent=agent,
                workspace=workspace,
                registry=registry,
                profile=profile,
                config_digest=digest,
                warnings=warnings,
            )
            self._handles[info.session_id] = handle
            if subagent_settings.enabled:
                self._ensure_orchestrator()
            return handle
        except BaseException:
            self._release_lease(info.session_id)
            self._workspace_by_session.pop(info.session_id, None)
            self._skill_catalogs.pop(info.session_id, None)
            self.workspaces.drop(info.session_id)
            self.tool_registries.drop(info.session_id)
            raise

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
        inherit_skills = cfg.agent.subagents.inherit_skills
        skill_index = (
            parent_catalog.index_text
            if inherit_skills and parent_catalog is not None else ""
        )

        agent = None
        try:
            self._workspace_by_session[child_id] = workspace
            if inherit_skills and parent_catalog is not None:
                self._skill_catalogs[child_id] = parent_catalog

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
