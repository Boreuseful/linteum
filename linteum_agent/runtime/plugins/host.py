"""``PluginHost``: discovery, two-phase load, quiesce/drain unload (§8).

The host is the only mutable piece of the plugin layer.  It owns a per-process
state machine, materialises skills/modes for the catalogs, and publishes
``system.plugin.*`` lifecycle events on the reserved ``host:<instance-id>``
partition (D8 / §9.2).  It never subscribes to the bus and never imports
``config``/``gateway``/``storage``.

Locking (§8.9):

* ``_lock`` protects the record map and ``_active``; no I/O and no drain waits
  happen while it is held.
* ``_lifecycle_locks[id]`` serialises load/unload for one plugin; different
  plugins may transition in parallel.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import hashlib
import inspect
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, Mapping, Optional, Sequence

from ...bus.event import Event, uuid7
from ..modes.model import ModePackage
from ..skills.model import SkillPackage
from ..skills.source import PRIORITY_PLUGIN
from ..tools.spec import ToolSpec
from .config import resolve_plugin_config, resolve_plugin_secrets
from .context import PluginContext, Staging
from .errors import (
    PluginBusy,
    PluginConfigError,
    PluginConflict,
    PluginDisabled,
    PluginError,
    PluginLoadError,
    PluginManifestError,
    PluginNamespaceError,
    PluginNotFound,
    PluginQuarantined,
    PluginTimeout,
    as_plugin_error,
)
from .events import (
    LOCATION_RUNTIME,
    NAMESPACE_VIOLATION_THRESHOLD,
    PluginRuntime,
)
from .loader import import_and_setup, run_cleanup, validate_staged
from .manifest import read_plugin_manifest, scan_plugins
from .model import (
    PLUGIN_ID_PATTERN,
    STATE_ACTIVE,
    STATE_DISABLED,
    STATE_DISCOVERED,
    STATE_FAILED,
    STATE_INVALID,
    STATE_LOADING,
    STATE_QUARANTINED,
    STATE_QUIESCING,
    STATE_UNLOADED,
    PluginAllowResult,
    PluginCatalog,
    PluginChange,
    PluginIssue,
    PluginLimits,
    PluginManifest,
    PluginSummary,
)

#: In-flight states that a directory rescan must not overwrite; quarantined
#: records keep their error/revoked state until an explicit load (§6.7).
_PROTECTED_STATES = frozenset({
    STATE_ACTIVE, STATE_QUIESCING, STATE_LOADING, STATE_QUARANTINED,
})

#: Stable ``location`` strings embedded in plugin ``error_info``.
LOCATION_VALIDATE = "linteum_agent.runtime.plugins.manifest.read_plugin_manifest"
LOCATION_CONFIG = "linteum_agent.runtime.plugins.config.resolve_plugin_config"
LOCATION_SETUP = "linteum_agent.runtime.plugins.loader.import_and_setup"
LOCATION_COMMIT = "linteum_agent.runtime.plugins.host.PluginHost._commit"


@dataclass
class _Record:
    """Mutable host-side record for one discovered plugin id."""

    manifest: PluginManifest
    source: str
    root: Path
    allowed: bool
    state: str
    error: Optional[PluginError] = None
    warnings: list[str] = field(default_factory=list)
    digest: str = ""
    #: Top-level config key -> ``"default" | "file"`` (values never stored).
    config_sources: tuple[tuple[str, str], ...] = ()
    #: Declared and resolved secret names only (values never stored).
    secret_names: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class LoadedPlugin:
    """The committed result of one successful load.

    Holds the live module/context/callables, so it must never enter an event
    payload, ``AgentState`` or a checkpoint.  ``runtime`` is the per-plugin
    event runtime attached by a later task (§6), or ``None``.
    """

    manifest: PluginManifest
    module: Any
    context: PluginContext
    tools: tuple[ToolSpec, ...]
    callbacks: tuple[Callable[[], Any], ...]
    digest: str
    config_sources: tuple[tuple[str, str], ...] = ()
    secret_names: tuple[str, ...] = ()
    runtime: Any = None


class PluginHost:
    """Discover, load and unload in-process capability plugins."""

    def __init__(
        self,
        bus=None,
        *,
        dirs: Sequence[Path] = (),
        allow: Sequence[str] = (),
        limits: Optional[PluginLimits] = None,
        enabled: bool = True,
        reserved_tool_names: Iterable[str] = (),
        on_drain: Optional[
            Callable[[str, tuple[str, ...], int], Awaitable[int] | int]
        ] = None,
        log: Optional[Callable[[dict], None]] = None,
        instance_id: Optional[str] = None,
        config: Optional[Mapping[str, Mapping[str, Any]]] = None,
        secrets: Optional[Mapping[str, Mapping[str, str]]] = None,
        environ: Optional[Mapping[str, str]] = None,
    ) -> None:
        self._bus = bus
        self._dirs: tuple[Path, ...] = tuple(Path(item) for item in dirs)
        self._allow: tuple[str, ...] = tuple(allow)
        self.limits = limits or PluginLimits()
        self._enabled = bool(enabled)
        self._reserved = frozenset(reserved_tool_names)
        self._on_drain = on_drain
        self._log_sink = log
        self.instance_id = instance_id or uuid7()
        self.host_partition = f"host:{self.instance_id}"
        # Deep per-plugin copies: the host never mutates the caller's maps and
        # later nested mutations cannot leak into a reload.
        self._config: dict[str, dict[str, Any]] = {
            str(plugin_id): copy.deepcopy(dict(values))
            for plugin_id, values in (config or {}).items()
        }
        self._secrets: dict[str, dict[str, str]] = {
            str(plugin_id): copy.deepcopy(dict(values))
            for plugin_id, values in (secrets or {}).items()
        }
        # Held live on purpose: every ``load`` re-reads current values (§4.3).
        self._environ: Mapping[str, str] = (
            os.environ if environ is None else environ
        )

        self._records: dict[str, _Record] = {}
        self._invalid: dict[str, PluginIssue] = {}
        self._active: dict[str, LoadedPlugin] = {}
        self._catalog_warnings: tuple[str, ...] = ()

        self._lock = asyncio.Lock()
        self._start_lock = asyncio.Lock()
        self._lifecycle_locks: dict[str, asyncio.Lock] = {}
        self._started = False
        self._closed = False

    # -- configuration ------------------------------------------------------
    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def allow(self) -> tuple[str, ...]:
        return self._allow

    @property
    def reserved_tool_names(self) -> frozenset[str]:
        return self._reserved

    # -- allow management (§5.2) ---------------------------------------------
    async def set_allowed(
        self, ids: Sequence[str], *, mode: str = "deferred",
    ) -> PluginAllowResult:
        """Change the runtime allowlist without persisting it (§5.2).

        Validation is atomic: the mode, id shape/uniqueness and every *added*
        id's record/state are checked before ``_allow`` or any record changes.
        ``deferred`` only rewrites the allowlist — nothing loads or unloads and
        the change takes effect on the next start.  ``apply`` additionally
        loads added ids in request order, then unloads removed ids in the
        previous allow order followed by every remaining active id missing
        from the allowlist — including ids an earlier ``deferred`` call
        already dropped (in load order) — sequentially; a per-id failure
        records ``action="failed"`` and never rolls the allowlist back.
        Removed ids may be stale or unknown so stale allow entries can be
        cleaned up.

        This method publishes no management event; persistence and the
        ``system.plugin.management`` audit belong to the gateway layer
        (§5.4 / §5.8).
        """
        if not self._enabled:
            raise PluginDisabled(
                "plugins are disabled", key="agent.plugins.enabled",
            )
        if self._closed:
            raise PluginLoadError("plugin host is closed", key="plugin_host")
        if mode not in ("deferred", "apply"):
            raise PluginConfigError(
                f"invalid set_allowed mode: {mode!r}",
                key="mode", hint="expected 'deferred' or 'apply'",
            )
        requested = tuple(ids)
        for plugin_id in requested:
            if (
                not isinstance(plugin_id, str)
                or PLUGIN_ID_PATTERN.match(plugin_id) is None
            ):
                raise PluginManifestError(
                    f"invalid plugin id: {plugin_id!r}",
                    key="agent.plugins.allow",
                )
        requested_set = set(requested)
        if len(requested_set) != len(requested):
            raise PluginManifestError(
                "duplicate plugin id in the allow list",
                key="agent.plugins.allow",
            )

        async with self._lock:
            old_allow = self._allow
            old_set = set(old_allow)
            changed = requested != old_allow
            added = [item for item in requested if item not in old_set]
            removed = [item for item in old_allow if item not in requested_set]
            if mode == "apply":
                # A deferred removal leaves the dropped plugin active in
                # memory, so an apply of the (possibly identical) allowlist
                # must still unload every active plugin missing from
                # ``requested`` — including ids no longer present in
                # ``old_allow``.  Old-allow removals run first (existing
                # behavior); the additional active orphans follow in
                # ``_active`` insertion order (load order, §5.3).
                removed_set = set(removed)
                removed.extend(
                    plugin_id
                    for plugin_id in self._active
                    if plugin_id not in requested_set
                    and plugin_id not in removed_set
                )
            # Every added id must be discovered and parseable before the
            # allowlist moves (atomic validation, §5.3).
            for plugin_id in added:
                record = self._records.get(plugin_id)
                if record is None:
                    raise PluginNotFound(
                        f"plugin not found: {plugin_id!r}", key="plugin_id",
                    )
                if record.state == STATE_INVALID:
                    hint = (
                        record.error.hint if record.error is not None else ""
                    )
                    raise PluginManifestError(
                        f"plugin {plugin_id!r} has an invalid manifest",
                        key="plugin.yaml", hint=hint,
                    )
            # Commit the allowlist and the per-record flags before any
            # load/unload runs: a later failure never rolls the allow truth
            # back (§5.3).
            self._allow = requested
            for plugin_id, record in self._records.items():
                record.allowed = plugin_id in requested_set
            if mode == "deferred":
                for plugin_id in removed:
                    record = self._records.get(plugin_id)
                    if record is not None and record.state != STATE_ACTIVE:
                        record.state = STATE_DISABLED

        changes: list[PluginChange] = []
        any_failed = False

        for plugin_id in requested:
            if plugin_id in old_set:
                changes.append(PluginChange(
                    id=plugin_id,
                    action="unchanged",
                    state=self._record_state(plugin_id),
                ))
                continue
            if mode == "deferred":
                changes.append(PluginChange(
                    id=plugin_id,
                    action="deferred",
                    state=self._record_state(plugin_id),
                ))
                continue
            # apply: load the newly allowed id (per-id serial, in order)
            try:
                await self.load(plugin_id)
            except asyncio.CancelledError:
                raise
            except PluginError as exc:
                error: Optional[PluginError] = exc
            except Exception as exc:  # noqa: BLE001 - one plugin must not stop the batch
                error = as_plugin_error(exc)
            else:
                error = None
            state = self._record_state(plugin_id)
            if error is None and state == STATE_ACTIVE:
                changes.append(PluginChange(
                    id=plugin_id, action="load", state=state,
                ))
            else:
                any_failed = True
                changes.append(PluginChange(
                    id=plugin_id, action="failed", state=state,
                    error=self._change_error(plugin_id, error),
                ))

        for plugin_id in removed:
            if mode == "deferred":
                changes.append(PluginChange(
                    id=plugin_id,
                    action="deferred",
                    state=self._record_state(plugin_id),
                ))
                continue
            # apply: unload a removed id that is still active.  Holding the
            # per-plugin lifecycle lock across the state re-read and the
            # unload serialises against an in-flight load, so an id that was
            # loading when the allowlist moved is unloaded after it commits
            # instead of being skipped and left active outside the allow
            # (§5.3).  A stale or non-active record is already out of the
            # runtime picture.
            async with self._lifecycle_lock(plugin_id):
                record = self._records.get(plugin_id)
                if record is None or record.state != STATE_ACTIVE:
                    changes.append(PluginChange(
                        id=plugin_id,
                        action="unload",
                        state=self._record_state(plugin_id),
                    ))
                    continue
                try:
                    await self._unload_locked(plugin_id)
                except asyncio.CancelledError:
                    raise
                except PluginError as exc:
                    error = exc
                except Exception as exc:  # noqa: BLE001 - one plugin must not stop the batch
                    error = as_plugin_error(exc)
                else:
                    error = None
                state = self._record_state(plugin_id)
                if error is None and state != STATE_ACTIVE:
                    changes.append(PluginChange(
                        id=plugin_id, action="unload", state=state,
                    ))
                else:
                    any_failed = True
                    changes.append(PluginChange(
                        id=plugin_id, action="failed", state=state,
                        error=self._change_error(plugin_id, error),
                    ))

        summaries = tuple(
            summary
            for summary in (self.get(item) for item in requested)
            if summary is not None
        )
        return PluginAllowResult(
            allow=requested,
            mode=mode,
            changed=changed,
            restart_required=changed if mode == "deferred" else any_failed,
            changes=tuple(changes),
            summaries=summaries,
        )

    def _record_state(self, plugin_id: str) -> str:
        """Current record state, or ``disabled`` for a never-discovered id."""
        record = self._records.get(plugin_id)
        return record.state if record is not None else STATE_DISABLED

    def _change_error(
        self, plugin_id: str, error: Optional[PluginError],
    ) -> dict:
        """Structured error dict for a failed change (raised error, else the
        record's own failure, else a generic target-state error)."""
        if error is None:
            record = self._records.get(plugin_id)
            error = record.error if record is not None else None
        if error is None:
            error = PluginLoadError(
                f"plugin {plugin_id!r} did not reach its target state",
                key="state",
            )
        return error.to_dict()

    def _lifecycle_lock(self, plugin_id: str) -> asyncio.Lock:
        lock = self._lifecycle_locks.get(plugin_id)
        if lock is None:
            lock = asyncio.Lock()
            self._lifecycle_locks[plugin_id] = lock
        return lock

    def _log(self, record: dict) -> None:
        if self._log_sink is None:
            return
        with contextlib.suppress(Exception):
            self._log_sink(record)

    async def _publish(self, etype: str, payload: dict) -> None:
        """Publish a lifecycle event; observability failures never change state."""
        if self._bus is None:
            return
        try:
            await self._bus.publish(Event.create(
                etype, payload, session_id=self.host_partition,
            ))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - event is observation, not state
            self._log({
                "kind": "plugin", "level": "warning",
                "msg": f"{etype} publish failed: {type(exc).__name__}: {exc}",
                "ts": time.time(),
            })

    async def _publish_session(
        self, etype: str, payload: dict, *, session_id: str,
        cause_id: Optional[str] = None,
    ) -> None:
        """Publish a hook fact on the triggering session's partition (§6.8)."""
        if self._bus is None:
            return
        try:
            await self._bus.publish(Event.create(
                etype, payload, session_id=session_id, cause_id=cause_id,
            ))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - event is observation, not state
            self._log({
                "kind": "plugin", "level": "warning",
                "msg": f"{etype} publish failed: {type(exc).__name__}: {exc}",
                "ts": time.time(),
            })

    # -- runtime failure callbacks (§6.7) -----------------------------------
    def _runtime_failure_callback(self, plugin_id: str) -> Callable[..., Any]:
        """Quarantine callback for repeated runtime failures (A.4).

        ``reason`` is a stable classifier (``"consecutive_failures"`` or
        ``"abandoned_threads"``); the descriptive detail rides on ``exc``.
        """

        async def on_failure(reason: str, exc: BaseException) -> None:
            error = PluginQuarantined(
                f"plugin {plugin_id!r} quarantined after repeated failures: "
                f"{reason}",
                detail=str(exc), exc_type=type(exc).__name__,
                context={"plugin_id": plugin_id, "reason": reason},
            )
            await self.quarantine(
                plugin_id,
                reason=reason,
                error_info=error.to_error_info(location=LOCATION_RUNTIME),
            )

        return on_failure

    def _runtime_namespace_callback(self, plugin_id: str) -> Callable[..., Any]:
        """Quarantine callback for three namespace violations (§6.3 / §6.7)."""

        async def on_namespace_violation(count: int) -> None:
            if count < NAMESPACE_VIOLATION_THRESHOLD:
                return
            error = PluginNamespaceError(
                f"{count} namespace violations by plugin {plugin_id!r}",
                context={"plugin_id": plugin_id, "count": count},
            )
            await self.quarantine(
                plugin_id,
                reason="namespace_violation",
                error_info=error.to_error_info(location=LOCATION_RUNTIME),
            )

        return on_namespace_violation

    async def _fail_config(
        self,
        plugin_id: str,
        record: "_Record",
        exc: PluginConfigError,
    ) -> None:
        """Record a config/secret failure: warning, failed state, event (§4.6).

        Warnings carry the plugin id and config key / secret name only; config
        values and secret values (or their ``env:`` sources) never appear.
        Secret errors are marked by :func:`resolve_plugin_secrets`; key names
        are never sniffed (``secrets_mode`` is an ordinary config key).
        """
        key = exc.key or "config"
        if exc.context.get("secret") is True:
            name = key.rsplit(".", 1)[-1]
            warning = f"plugin.secret_missing: {plugin_id} {name}"
        else:
            warning = f"plugin.config_invalid: {plugin_id} {key}"
        async with self._lock:
            record.state = STATE_FAILED
            record.error = exc
            record.warnings.append(warning)
        await self._publish_failed(plugin_id, "config", exc)

    async def _publish_failed(
        self, plugin_id: Optional[str], phase: str, exc: PluginError,
    ) -> None:
        location = {
            "validate": LOCATION_VALIDATE,
            "config": LOCATION_CONFIG,
            "setup": LOCATION_SETUP,
            "commit": LOCATION_COMMIT,
        }.get(phase, "")
        await self._publish("system.plugin.failed", {
            "id": plugin_id,
            "phase": phase,
            "code": exc.code,
            "error_info": exc.to_error_info(location=location),
        })

    # -- discovery ----------------------------------------------------------
    async def discover(self) -> PluginCatalog:
        """Re-scan configured containers; never import plugin code."""
        if not self._enabled:
            async with self._lock:
                self._records.clear()
                self._invalid.clear()
                self._active.clear()
                self._catalog_warnings = ("plugins are disabled",)
            return PluginCatalog(warnings=("plugins are disabled",))

        catalog = await asyncio.to_thread(
            scan_plugins, self._dirs, limits=self.limits,
        )
        warnings = list(catalog.warnings)
        for plugin_id in self._allow:
            if plugin_id not in {item.id for item in catalog.manifests}:
                warnings.append(
                    f"plugin.not_found: {plugin_id!r} from agent.plugins.allow"
                )
        async with self._lock:
            previous = self._records
            records: dict[str, _Record] = {}
            for manifest in catalog.manifests:
                old = previous.get(manifest.id)
                if old is not None and old.state in _PROTECTED_STATES:
                    records[manifest.id] = old
                    continue
                records[manifest.id] = _Record(
                    manifest=manifest,
                    source=manifest.source,
                    root=manifest.root,
                    allowed=manifest.id in self._allow,
                    state=(
                        STATE_DISCOVERED
                        if manifest.id in self._allow else STATE_DISABLED
                    ),
                    warnings=list(manifest.warnings),
                    digest=manifest.digest,
                )
            # An active plugin whose directory vanished stays loaded in memory;
            # refresh never rewrites a protected record (§8.9).
            for plugin_id, record in previous.items():
                if plugin_id not in records and record.state in _PROTECTED_STATES:
                    records[plugin_id] = record
            invalid: dict[str, PluginIssue] = {}
            for issue in catalog.invalid:
                if issue.id and issue.id not in records:
                    # Keep a record keyed by the parseable id so a fixed
                    # manifest can be reloaded without a restart (§8.4).
                    error = PluginError(issue.error, key="plugin.yaml",
                                        hint=issue.hint)
                    error.code = issue.code
                    records[issue.id] = _Record(
                        manifest=PluginManifest(
                            id=issue.id,
                            version="",
                            root=issue.root or Path("."),
                            source=issue.source,
                        ),
                        source=issue.source,
                        root=issue.root or Path("."),
                        allowed=issue.id in self._allow,
                        state=STATE_INVALID,
                        error=error,
                        warnings=[],
                        digest="",
                    )
                    continue
                invalid[issue.source] = issue
            self._records = records
            self._invalid = invalid
            self._catalog_warnings = tuple(dict.fromkeys(warnings))
        return catalog

    # Backwards/API-friendly alias (§11.1).
    async def refresh(self) -> PluginCatalog:
        return await self.discover()

    async def start(self) -> "PluginHost":
        """Discover and load every allowed plugin in configuration order."""
        async with self._start_lock:
            if self._started or self._closed:
                return self
            self._started = True
            await self.discover()
            if not self._enabled:
                return self
            await self._load_allowed()
        return self

    async def _load_allowed(self) -> None:
        for plugin_id in self._allow:
            record = self._records.get(plugin_id)
            if record is None or not record.allowed:
                continue
            try:
                await self.load(plugin_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - one plugin must not stop start
                self._log({
                    "kind": "plugin", "level": "warning",
                    "msg": f"plugin {plugin_id!r} failed to load: "
                           f"{type(exc).__name__}: {exc}",
                    "ts": time.time(),
                })

    async def close(self) -> None:
        """Unload every active plugin; the bus is still usable at this point."""
        if self._closed:
            return
        self._closed = True
        for plugin_id in list(self._active):
            try:
                await self.unload(plugin_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - shutdown must continue
                self._log({
                    "kind": "plugin", "level": "warning",
                    "msg": f"plugin {plugin_id!r} unload at close failed: "
                           f"{type(exc).__name__}: {exc}",
                    "ts": time.time(),
                })
        # Belt and braces: an unload that failed midway must not leave bus
        # subscriptions behind (§9.4).
        for loaded in self._active.values():
            if loaded.runtime is not None:
                await loaded.runtime.close()

    # -- load ---------------------------------------------------------------
    async def load(self, plugin_id: str) -> PluginSummary:
        """Load one plugin, re-reading its manifest every time (§8.4)."""
        if not self._enabled:
            raise PluginDisabled("plugins are disabled", key="agent.plugins.enabled")
        if self._closed:
            raise PluginLoadError("plugin host is closed", key="plugin_host")

        async with self._lifecycle_lock(plugin_id):
            # The allow check sits under the per-plugin lifecycle lock so a
            # concurrent ``set_allowed`` removal serialises against it (§5.3):
            # a load that loses the race raises instead of committing an
            # active plugin outside the final allow.
            if plugin_id not in self._allow:
                raise PluginDisabled(
                    f"plugin {plugin_id!r} is not in agent.plugins.allow",
                    key="agent.plugins.allow",
                )
            async with self._lock:
                record = self._records.get(plugin_id)
                if record is None:
                    raise PluginNotFound(f"plugin not found: {plugin_id!r}")
                if record.state == STATE_ACTIVE:
                    return self._summary(record)
                if record.state in (STATE_LOADING, STATE_QUIESCING):
                    raise PluginBusy(
                        f"plugin {plugin_id!r} is busy ({record.state})",
                        key="state",
                    )
                record.state = STATE_LOADING
                record.error = None
                # Sources are shown for active plugins only (§4.5); clear them
                # up front so an invalid/config-failed load cannot surface a
                # previous load's snapshot.
                record.config_sources = ()
                record.secret_names = ()
                root, source = record.root, record.source

            # ① re-read and re-validate the manifest (no import yet)
            try:
                manifest = await asyncio.to_thread(
                    read_plugin_manifest, root,
                    source=source, limits=self.limits,
                )
            except PluginError as exc:
                async with self._lock:
                    record.state = STATE_INVALID
                    record.error = exc
                await self._publish_failed(plugin_id, "validate", exc)
                return self._summary(record)
            async with self._lock:
                record.manifest = manifest
                record.warnings = list(manifest.warnings)

            # ② resolve config/secrets from the host snapshot (§4.3); a failure
            # is terminal for this load and never imports plugin code.
            try:
                values, sources = resolve_plugin_config(
                    manifest, self._config.get(plugin_id, {}),
                )
                secret_values = resolve_plugin_secrets(
                    manifest, self._secrets.get(plugin_id, {}), self._environ,
                )
            except PluginConfigError as exc:
                await self._fail_config(plugin_id, record, exc)
                return self._summary(record)
            async with self._lock:
                record.config_sources = tuple(sources.items())
                record.secret_names = tuple(secret_values)

            await self._publish("system.plugin.loading", {
                "id": plugin_id,
                "version": manifest.version,
                "phase": "setup",
            })

            # ③ import + setup into staging, then validate the declarations
            staging = Staging(plugin_id)
            runtime = PluginRuntime(
                plugin_id,
                self._bus,
                host_partition=self.host_partition,
                limits=manifest.runtime_limits,
                ordering=manifest.ordering,
                publish_patterns=manifest.publishes,
                subscribe_patterns=manifest.subscribes,
                is_active=lambda: record.state == STATE_ACTIVE,
                on_failure=self._runtime_failure_callback(plugin_id),
                on_namespace_violation=self._runtime_namespace_callback(
                    plugin_id
                ),
                log=self._log_sink,
            )
            context = PluginContext(
                manifest, staging,
                config=values,
                secrets=secret_values,
                runtime=runtime,
                log=self._log_sink,
            )
            try:
                module = await import_and_setup(
                    manifest, context,
                    timeout_ms=self.limits.load_timeout_ms,
                    log=self._log_sink,
                )
                tools, tool_warnings = validate_staged(staging, manifest)
            except asyncio.CancelledError as exc:
                await run_cleanup(
                    staging.snapshot()[1],
                    timeout_ms=self.limits.unload_timeout_ms,
                    log=self._log_sink,
                )
                async with self._lock:
                    record.state = STATE_FAILED
                    record.error = as_plugin_error(exc)
                raise
            except BaseException as exc:
                await run_cleanup(
                    staging.snapshot()[1],
                    timeout_ms=self.limits.unload_timeout_ms,
                    log=self._log_sink,
                )
                error = as_plugin_error(exc)
                async with self._lock:
                    record.state = STATE_FAILED
                    record.error = error
                    if isinstance(error, PluginTimeout):
                        record.warnings.append(
                            f"plugin.load_timeout_abandoned: {plugin_id}"
                        )
                await self._publish_failed(plugin_id, "setup", error)
                if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                    raise
                return self._summary(record)

            # ④ commit atomically; conflict checks share the state lock
            _, callbacks = staging.snapshot()
            digest = _plugin_digest(manifest, tools)
            async with self._lock:
                if self._closed:
                    record.state = STATE_FAILED
                    record.error = PluginLoadError(
                        "plugin host closed during load", key="plugin_host",
                    )
                    error = record.error
                else:
                    try:
                        self._assert_no_conflict(plugin_id, tools)
                        runtime.install(
                            staging.subscriptions(), staging.hooks(),
                        )
                    except PluginConflict as exc:
                        record.state = STATE_FAILED
                        record.error = exc
                        error = exc
                    except Exception as exc:  # noqa: BLE001 - a refused subscription
                        record.state = STATE_FAILED
                        record.error = as_plugin_error(exc)
                        error = record.error
                    else:
                        loaded = LoadedPlugin(
                            manifest=manifest,
                            module=module,
                            context=context,
                            tools=tuple(tools),
                            callbacks=tuple(callbacks),
                            digest=digest,
                            config_sources=tuple(sources.items()),
                            secret_names=tuple(secret_values),
                            runtime=runtime,
                        )
                        self._active[plugin_id] = loaded
                        record.state = STATE_ACTIVE
                        record.error = None
                        record.digest = digest
                        record.warnings = [
                            *manifest.warnings, *tool_warnings,
                        ]
                        # Commit the context: close setup registration and
                        # enable ctx.publish (§6.3).
                        context.seal(committed=True)
            if record.state != STATE_ACTIVE:
                # A failed commit must not leave bus subscriptions behind (§9.1).
                await runtime.close()
                await run_cleanup(
                    callbacks,
                    timeout_ms=self.limits.unload_timeout_ms,
                    log=self._log_sink,
                )
                await self._publish_failed(plugin_id, "commit", error)
                return self._summary(record)

            await self._publish("system.plugin.loaded", {
                "id": plugin_id,
                "version": manifest.version,
                "tools": [item.name for item in tools],
                "skills": list(manifest.skill_names),
                "modes": list(manifest.mode_names),
                "digest": digest,
            })
            return self._summary(record)

    def _assert_no_conflict(
        self, plugin_id: str, tools: Sequence[ToolSpec],
    ) -> None:
        for spec in tools:
            if spec.name in self._reserved:
                raise PluginConflict(
                    f"tool {spec.name!r} conflicts with a host-reserved name",
                    key="capabilities.tools",
                    context={"plugin_id": plugin_id, "tool": spec.name,
                             "conflict": "reserved"},
                )
            for other_id, loaded in self._active.items():
                if other_id == plugin_id:
                    continue
                if any(item.name == spec.name for item in loaded.tools):
                    raise PluginConflict(
                        f"tool {spec.name!r} conflicts with active plugin "
                        f"{other_id!r}",
                        key="capabilities.tools",
                        context={"plugin_id": plugin_id, "tool": spec.name,
                                 "conflict": other_id},
                    )

    # -- unload -------------------------------------------------------------
    async def unload(
        self, plugin_id: str, *, grace_ms: Optional[int] = None,
    ) -> PluginSummary:
        """quiesce → drain → revoke, in that order (§8.5)."""
        async with self._lifecycle_lock(plugin_id):
            return await self._unload_locked(plugin_id, grace_ms=grace_ms)

    async def _unload_locked(
        self, plugin_id: str, *, grace_ms: Optional[int] = None,
    ) -> PluginSummary:
        """Body of :meth:`unload`; the caller must hold the plugin's
        ``_lifecycle_lock`` (§5.3)."""
        grace = self.limits.drain_grace_ms if grace_ms is None else grace_ms
        async with self._lock:
            loaded = self._active.get(plugin_id)
            record = self._records.get(plugin_id)
            if record is None:
                raise PluginNotFound(
                    f"plugin is not loaded: {plugin_id!r}", key="plugin_id",
                )
            if record.state == STATE_QUARANTINED and loaded is None:
                # Already revoked by quarantine: transition only (§9.3).
                record.state = STATE_UNLOADED
                record.error = None
                return self._summary(record)
            if loaded is None:
                raise PluginNotFound(
                    f"plugin is not loaded: {plugin_id!r}", key="plugin_id",
                )
            if record.state != STATE_ACTIVE:
                raise PluginBusy(
                    f"plugin {plugin_id!r} is busy ({record.state})",
                    key="state",
                )
            record.state = STATE_QUIESCING
            names = tuple(item.name for item in loaded.tools)

        # 1) stop offering the capability to new catalogs/sessions
        await self._publish("system.plugin.quiescing", {
            "id": plugin_id, "tools": list(names),
        })

        # 2) revoke bus subscriptions and hook handlers (§9.2)
        if loaded.runtime is not None:
            await loaded.runtime.close()

        # 3) drain in-flight calls in live registries
        remaining = 0
        if self._on_drain is not None:
            try:
                result = self._on_drain(plugin_id, names, grace)
                if inspect.isawaitable(result):
                    result = await result
                remaining = int(result or 0)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - drain failure must not wedge unload
                self._log({
                    "kind": "plugin", "level": "warning",
                    "msg": f"plugin {plugin_id!r} drain failed: "
                           f"{type(exc).__name__}: {exc}",
                    "ts": time.time(),
                })
        if remaining:
            message = (
                f"plugin.drain_timeout: {plugin_id} still had "
                f"{remaining} in-flight call(s)"
            )
            async with self._lock:
                record.warnings.append(message)

        # 4) run unload callbacks (reverse order, shared budget)
        await run_cleanup(
            loaded.callbacks,
            timeout_ms=self.limits.unload_timeout_ms,
            log=self._log_sink,
        )

        async with self._lock:
            self._active.pop(plugin_id, None)
            record.state = STATE_UNLOADED
            record.error = None
            # Config sources/secret names are an active-plugin view (§4.5).
            record.config_sources = ()
            record.secret_names = ()

        await self._publish("system.plugin.unloaded", {
            "id": plugin_id,
            "drained": remaining == 0,
            "inflight_remaining": remaining,
        })
        return self._summary(record)

    # -- quarantine ---------------------------------------------------------
    async def quarantine(
        self, plugin_id: str, *, reason: str,
        error_info: Optional[Mapping[str, Any]],
    ) -> PluginSummary:
        """Revoke a plugin's live capabilities after repeated failures (§6.7).

        Revokes bus subscriptions and hooks, drains in-flight tool calls, runs
        unload callbacks and publishes ``system.plugin.quarantined``.  Idempotent:
        quarantining a non-active plugin returns its summary without a second
        teardown.  There is no automatic recovery — an explicit ``load`` /
        ``reload`` retries (§9.3).
        """
        grace = self.limits.drain_grace_ms
        async with self._lifecycle_lock(plugin_id):
            async with self._lock:
                loaded = self._active.get(plugin_id)
                record = self._records.get(plugin_id)
                if record is None:
                    raise PluginNotFound(
                        f"plugin not found: {plugin_id!r}", key="plugin_id",
                    )
                if loaded is None or record.state != STATE_ACTIVE:
                    return self._summary(record)
                record.state = STATE_QUARANTINED
                record.error = PluginQuarantined(
                    f"plugin {plugin_id!r} quarantined: {reason}",
                    hint="load the plugin again to retry",
                )
                names = tuple(item.name for item in loaded.tools)

            # 1) revoke bus subscriptions and hook handlers (§6.7)
            if loaded.runtime is not None:
                await loaded.runtime.close()

            # 2) drain in-flight calls in live registries
            remaining = 0
            if self._on_drain is not None:
                try:
                    result = self._on_drain(plugin_id, names, grace)
                    if inspect.isawaitable(result):
                        result = await result
                    remaining = int(result or 0)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - drain failure must not wedge quarantine
                    self._log({
                        "kind": "plugin", "level": "warning",
                        "msg": f"plugin {plugin_id!r} drain failed: "
                               f"{type(exc).__name__}: {exc}",
                        "ts": time.time(),
                    })
            if remaining:
                message = (
                    f"plugin.drain_timeout: {plugin_id} still had "
                    f"{remaining} in-flight call(s)"
                )
                async with self._lock:
                    record.warnings.append(message)

            # 3) run unload callbacks (reverse order, shared budget)
            await run_cleanup(
                loaded.callbacks,
                timeout_ms=self.limits.unload_timeout_ms,
                log=self._log_sink,
            )

            async with self._lock:
                self._active.pop(plugin_id, None)
                # Config sources/secret names are an active-plugin view (§4.5).
                record.config_sources = ()
                record.secret_names = ()

            # 4) the quarantine fact
            await self._publish("system.plugin.quarantined", {
                "id": plugin_id,
                "reason": reason,
                "error_info": dict(error_info or {}),
            })
            return self._summary(record)

    # -- hooks ---------------------------------------------------------------
    async def run_hook(
        self, name: str, payload: Mapping[str, Any], *,
        session_id: str, cause_id: Optional[str] = None,
        trace_id: Optional[str] = None,
    ) -> None:
        """Invoke every active handler for ``name`` in global order (§6.5).

        Handlers sort by ``(ordering.priority, plugin_id, registration_index)``
        and run sequentially.  ``fail_mode="closed"`` on the failing plugin
        stops the remaining handlers; ``open`` continues.  Outcomes are
        materialised as ``hook.<name>.applied`` / ``hook.<name>.failed`` facts
        on the triggering session's partition — a plugin failure never fails
        the session (D0.4-3).
        """
        if not self._started or self._closed:
            return
        entries: list[tuple[int, str, int, PluginRuntime, Callable[..., Any]]] = []
        for plugin_id, loaded in self._visible_active():
            runtime = loaded.runtime
            if runtime is None:
                continue
            for index, handler in enumerate(runtime.hook_handlers(name)):
                entries.append((
                    runtime.ordering.priority, plugin_id, index, runtime, handler,
                ))
        entries.sort(key=lambda entry: (entry[0], entry[1], entry[2]))
        for priority, plugin_id, index, runtime, handler in entries:
            outcome = await runtime.invoke_hook_handler(
                name, handler, payload,
                session_id=session_id, cause_id=cause_id, trace_id=trace_id,
            )
            if outcome.status == "ok" and outcome.output is not None:
                await self._publish_session(
                    f"hook.{name}.applied",
                    {
                        "plugin": plugin_id,
                        "priority": priority,
                        "handler_index": index,
                        "output": outcome.output,
                    },
                    session_id=session_id, cause_id=cause_id,
                )
            elif outcome.status == "failed":
                await self._publish_session(
                    f"hook.{name}.failed",
                    {
                        "plugin": plugin_id,
                        "priority": priority,
                        "handler_index": index,
                        "error_info": outcome.error_info or {},
                    },
                    session_id=session_id, cause_id=cause_id,
                )
                if runtime.ordering.fail_mode == "closed":
                    break
            # shed/skipped: no event, keep going

    # -- capability views ---------------------------------------------------
    def _ordered_active(self) -> list[tuple[str, LoadedPlugin]]:
        """Active plugins in allow order, then any stragglers by id (§6.2)."""
        order = {plugin_id: index for index, plugin_id in enumerate(self._allow)}
        return sorted(
            self._active.items(),
            key=lambda item: (order.get(item[0], len(order)), item[0]),
        )

    def _visible_active(self) -> list[tuple[str, LoadedPlugin]]:
        """Only ``active`` plugins; quiescing ones already stopped offering (§8.5)."""
        return [
            (plugin_id, loaded)
            for plugin_id, loaded in self._ordered_active()
            if (
                self._records.get(plugin_id) is not None
                and self._records[plugin_id].state == STATE_ACTIVE
            )
        ]

    def tool_specs(self) -> tuple[ToolSpec, ...]:
        """Every tool from every active plugin, in load order."""
        return tuple(
            spec
            for _, loaded in self._visible_active()
            for spec in loaded.tools
        )

    def skill_packages_with_warnings(
        self,
    ) -> tuple[list[SkillPackage], list[str]]:
        packages: list[SkillPackage] = []
        warnings: list[str] = []
        for plugin_id, loaded in self._visible_active():
            root_real = _real_root(loaded.manifest.root)
            for path in loaded.manifest.skill_dirs:
                name = path.name
                try:
                    if path.is_symlink() or not path.is_dir():
                        warnings.append(
                            f"plugin.skill_missing: {plugin_id}/{name}"
                        )
                        continue
                    skill_md = path / "SKILL.md"
                    if skill_md.is_symlink() or not skill_md.is_file():
                        warnings.append(
                            f"plugin.skill_missing: {plugin_id}/{name}"
                        )
                        continue
                    real = path.resolve(strict=True)
                    if root_real is not None and root_real not in real.parents:
                        warnings.append(
                            f"plugin.skill_missing: {plugin_id}/{name} escaped "
                            "the plugin root"
                        )
                        continue
                except OSError:
                    warnings.append(f"plugin.skill_missing: {plugin_id}/{name}")
                    continue
                packages.append(SkillPackage(
                    source_id=f"plugin:{plugin_id}",
                    priority=PRIORITY_PLUGIN,
                    root=path,
                ))
        return packages, warnings

    def skill_packages(self) -> tuple[SkillPackage, ...]:
        return tuple(self.skill_packages_with_warnings()[0])

    def mode_packages(self) -> tuple[ModePackage, ...]:
        """Materialise declared mode files for ``PluginModeSource`` (§7.2)."""
        packages: list[ModePackage] = []
        for plugin_id, loaded in self._visible_active():
            root_real = _real_root(loaded.manifest.root)
            for path in loaded.manifest.mode_paths:
                try:
                    if path.is_symlink() or not path.is_file():
                        self._log({
                            "kind": "plugin", "level": "warning",
                            "msg": f"plugin.mode_missing: {plugin_id}/{path.stem}",
                            "ts": time.time(),
                        })
                        continue
                    real = path.resolve(strict=True)
                    if root_real is not None and root_real not in real.parents:
                        continue
                except OSError:
                    continue
                packages.append(ModePackage(
                    name=path.stem,
                    path=path,
                    source_id=f"plugin:{plugin_id}",
                    priority=PRIORITY_PLUGIN,
                    builtin=False,
                ))
        return tuple(packages)

    # -- inventory / summaries ---------------------------------------------
    def inventory(self) -> tuple[dict, ...]:
        """Active plugin ids/versions/digests, sorted for determinism (§14.3)."""
        return tuple({
            "id": plugin_id,
            "version": loaded.manifest.version,
            "digest": loaded.digest,
        } for plugin_id, loaded in sorted(self._visible_active()))

    def inventory_digest(self) -> str:
        blob = json.dumps(
            list(self.inventory()), ensure_ascii=False, sort_keys=True,
            separators=(",", ":"),
        )
        return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def summaries(self) -> list[PluginSummary]:
        """Every discovered record plus invalid directories (no import)."""
        result = [self._summary(record) for record in self._records.values()]
        for issue in self._invalid.values():
            result.append(PluginSummary(
                # Best-effort id from the invalid manifest; plugin.list still
                # renders invalid entries by source (§11.2) but management
                # commands need the id to report E_PLUGIN_MANIFEST (§11.5.4).
                id=issue.id,
                version="",
                description="",
                state=STATE_INVALID,
                trust="",
                source=issue.source,
                error={
                    "code": issue.code,
                    "message": issue.error,
                    "hint": issue.hint,
                },
            ))
        return result

    def catalog_warnings(self) -> tuple[str, ...]:
        return self._catalog_warnings

    def get(self, plugin_id: str) -> Optional[PluginSummary]:
        record = self._records.get(plugin_id)
        return self._summary(record) if record is not None else None

    def _summary(self, record: _Record) -> PluginSummary:
        manifest = record.manifest
        loaded = self._active.get(manifest.id)
        tools = (
            tuple(item.name for item in loaded.tools)
            if loaded is not None else manifest.tool_names
        )
        active = loaded if record.state == STATE_ACTIVE else None
        config_sources = active.config_sources if active is not None else ()
        secret_names = active.secret_names if active is not None else ()
        error = None
        if record.error is not None:
            error = {
                "code": record.error.code,
                "message": str(record.error),
                "hint": record.error.hint,
            }
        return PluginSummary(
            id=manifest.id,
            version=manifest.version,
            description=manifest.description,
            state=record.state,
            trust=manifest.trust,
            source=record.source,
            tools=tools,
            skills=manifest.skill_names,
            modes=manifest.mode_names,
            digest=record.digest or manifest.digest,
            error=error,
            warnings=tuple(record.warnings),
            hooks=manifest.hooks,
            subscribes=manifest.subscribes,
            publishes=manifest.publishes,
            config=config_sources,
            secrets=secret_names,
        )


def _real_root(root: Path) -> Optional[Path]:
    try:
        return root.resolve(strict=True)
    except OSError:
        return None


def _plugin_digest(manifest: PluginManifest, tools: Sequence[ToolSpec]) -> str:
    """Digest of manifest bytes + actual tool names (no absolute paths)."""
    payload = json.dumps(
        {
            "manifest": manifest.digest,
            "tools": sorted(item.name for item in tools),
        },
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


__all__ = [
    "LOCATION_COMMIT",
    "LOCATION_CONFIG",
    "LOCATION_SETUP",
    "LOCATION_VALIDATE",
    "LoadedPlugin",
    "PluginHost",
]
