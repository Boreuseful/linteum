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
from typing import Any, Mapping, Optional, Protocol, runtime_checkable

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
    def _context_settings(cfg: AppConfig, skill_index: str = ""):
        blocks = (skill_index,) if skill_index else ()
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
            for session_id in list(self._handles):
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
        session_id = options.session_id or uuid7()

        async with self._registry_lock:
            handle = self._handles.get(session_id)
            if handle is not None:
                return handle

            info = await self.service.get_session(session_id)
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
            return handle
        info = await self.service.get_session(session_id)
        if info is None:
            raise SessionNotFound(session_id)
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
        # ★ One live reducer per session, process-wide and machine-wide (§6.1).
        await self._acquire_lease(info.session_id)
        try:
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
            kwargs = {
                "model": profile,
                "schemas": registry.schemas,
                "budget": budget,
                "ctx_builder": builder,
                "context_policy": policy,
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
        """Detach a session's reducer; durable history stays in the WAL."""
        handle = self._handles.pop(session_id, None)
        if handle is not None:
            handle.agent.close()
        self._workspace_by_session.pop(session_id, None)
        self._skill_catalogs.pop(session_id, None)
        self.workspaces.drop(session_id)
        self.tool_registries.drop(session_id)
        self.approvals.cancel(session_id=session_id)
        self._release_lease(session_id)  # another process may now take over

    async def delete_session(self, session_id: str) -> None:
        """Close and erase a session's catalogue/checkpoint rows."""
        self.close_session(session_id)
        await self.service.delete_session(session_id)

    # -- reads --------------------------------------------------------------
    async def list_sessions(self, *, limit: int = 50, offset: int = 0,
                            workspace: Optional[str] = None) -> list[SessionInfo]:
        return await self.service.list_sessions(
            limit=limit, offset=offset, workspace=workspace
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
