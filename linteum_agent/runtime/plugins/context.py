"""``PluginContext`` — the only host surface a capability plugin may use (§5.2).

The context is a deliberately small facade.  It exposes **no** bus, registry,
``AgentState`` or host reference: ``publish`` is a controlled channel for the
plugin's declared ``plugin.<id>.*`` types, delegated to a private host runtime
that stamps ``origin``/``seq``/``session_id``.  Setup-time state changes
(``provide_tool``, ``on``, ``on_unload``) write to a per-plugin staging area;
the host commits that staging atomically *after* ``setup`` returns (two-phase
load, §8.4).
"""

from __future__ import annotations

import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from ..tools.spec import ToolSpec
from .config import freeze_config
from .errors import (
    PluginConfigError,
    PluginConflict,
    PluginLoadError,
    PluginNamespaceError,
    PluginToolUndeclared,
)
from .model import PluginManifest


@dataclass(frozen=True, slots=True)
class _StagedHandler:
    """One staged hook handler or subscription; identity is the unsubscribe token.

    ``name`` is the hook name (without the ``hook.`` prefix) for hooks and the
    exact event type for subscriptions.  :meth:`Staging.add_hook` and
    :meth:`Staging.add_subscription` return the entry so the context's
    unsubscribe closure can remove exactly that registration.
    """

    name: str
    handler: Callable[..., Any]


@dataclass
class Staging:
    """Per-load staging area: tools, handlers and unload callbacks.

    ``setup`` may run from a worker thread (synchronous ``setup`` runs through
    ``asyncio.to_thread``), so every stream is guarded by a plain
    :class:`threading.Lock` rather than an asyncio lock.  ``snapshot`` keeps the
    two-tuple ``(tools, callbacks)`` contract the loader consumes; hooks and
    subscriptions are separate streams committed by the host (§6.3).
    """

    plugin_id: str
    tools: list[ToolSpec] = field(default_factory=list)
    callbacks: list[Callable[[], Any]] = field(default_factory=list)
    _subscriptions: list[_StagedHandler] = field(default_factory=list, repr=False)
    _hooks: list[_StagedHandler] = field(default_factory=list, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def add_tool(self, spec: ToolSpec) -> None:
        with self._lock:
            if any(item.name == spec.name for item in self.tools):
                raise PluginConflict(
                    f"tool {spec.name!r} was staged twice",
                    key="tools",
                    context={"plugin_id": self.plugin_id, "tool": spec.name},
                )
            self.tools.append(spec)

    def add_callback(self, callback: Callable[[], Any]) -> None:
        with self._lock:
            self.callbacks.append(callback)

    def add_subscription(
        self, pattern: str, handler: Callable[..., Any],
    ) -> _StagedHandler:
        entry = _StagedHandler(pattern, handler)
        with self._lock:
            self._subscriptions.append(entry)
        return entry

    def add_hook(self, name: str, handler: Callable[..., Any]) -> _StagedHandler:
        entry = _StagedHandler(name, handler)
        with self._lock:
            self._hooks.append(entry)
        return entry

    def _remove_subscription(self, entry: _StagedHandler) -> None:
        with self._lock:
            _discard(self._subscriptions, entry)

    def _remove_hook(self, entry: _StagedHandler) -> None:
        with self._lock:
            _discard(self._hooks, entry)

    def subscriptions(self) -> tuple[tuple[str, Callable[..., Any]], ...]:
        with self._lock:
            return tuple(
                (entry.name, entry.handler) for entry in self._subscriptions
            )

    def hooks(self) -> dict[str, tuple[Callable[..., Any], ...]]:
        with self._lock:
            grouped: dict[str, list[Callable[..., Any]]] = {}
            for entry in self._hooks:
                grouped.setdefault(entry.name, []).append(entry.handler)
            return {name: tuple(handlers) for name, handlers in grouped.items()}

    def snapshot(self) -> tuple[tuple[ToolSpec, ...], tuple[Callable[[], Any], ...]]:
        with self._lock:
            return tuple(self.tools), tuple(self.callbacks)


def _discard(entries: list[_StagedHandler], entry: _StagedHandler) -> None:
    """Remove one staging entry by identity (caller holds the staging lock)."""
    for index, staged in enumerate(entries):
        if staged is entry:
            del entries[index]
            return


def matches_publish_patterns(etype: str, patterns: tuple[str, ...]) -> bool:
    """True when ``etype`` matches one declared publish pattern (§6.3).

    Exact matching applies only to non-wildcard patterns; a wildcard pattern
    matches concrete suffixes only — never its literal text or another glob.
    Shared by :class:`PluginContext` (the setup-time gate) and the runtime's
    :meth:`~linteum_agent.runtime.plugins.events.PluginRuntime.publish` (the
    defense-in-depth gate).
    """
    for pattern in patterns:
        if pattern.endswith(".*"):
            prefix = pattern[:-1]  # keep the dot so the match is boundary-safe
            if (
                etype.startswith(prefix)
                and len(etype) > len(prefix)
                and not etype.endswith(".*")
            ):
                return True
        elif etype == pattern:
            return True
    return False


class PluginContext:
    """The host facade handed to ``setup(ctx)``.

    ``id``/``version``/``plugin_dir`` and the frozen ``config`` snapshot are
    read-only facts.  ``provide_tool``/``on`` fail immediately for a capability
    the manifest did not declare, which makes a typo a load failure instead of a
    silently missing capability.  ``publish`` is only enabled once the host
    calls :meth:`seal` with ``committed=True`` (§6.3).
    """

    def __init__(
        self,
        manifest: PluginManifest,
        staging: Staging,
        *,
        config: Optional[Mapping[str, Any]] = None,
        secrets: Optional[Mapping[str, str]] = None,
        runtime: Optional[Any] = None,
        log: Optional[Callable[[dict], None]] = None,
    ) -> None:
        self.id = manifest.id
        self.version = manifest.version
        self.plugin_dir: Path = manifest.root
        self.config: Mapping[str, Any] = freeze_config(config or {})
        self._manifest = manifest
        self._staging = staging
        self._declared = frozenset(item.name for item in manifest.tools)
        self._declared_hooks = frozenset(manifest.hooks)
        self._declared_subscribes = frozenset(manifest.subscribes)
        self._publish_patterns = tuple(manifest.publishes)
        self._declared_secrets = frozenset(manifest.needs_secrets)
        self._secrets = dict(secrets or {})
        self._runtime = runtime
        self._sealed = False
        self._committed = False
        self._log_sink = log

    # -- config / secrets ---------------------------------------------------
    def secret(self, name: str) -> str:
        """Return one resolved secret value; never log or echo it (§4.4)."""
        if name not in self._declared_secrets:
            raise PluginConfigError(
                f"secret {name!r} is not declared by the plugin",
                key=name,
                hint="declare it under needs_secrets in plugin.yaml",
                context={"plugin_id": self.id, "secret": name},
            )
        if name not in self._secrets:
            raise PluginConfigError(
                f"secret {name!r} was not resolved by the host",
                key=name,
                hint=f"set agent.plugins.secrets.{self.id}.{name}",
                context={"plugin_id": self.id, "secret": name},
            )
        return self._secrets[name]

    # -- tools --------------------------------------------------------------
    def provide_tool(self, spec: ToolSpec) -> None:
        """Stage one tool; the host validates and commits it after ``setup``."""
        if not isinstance(spec, ToolSpec):
            raise PluginLoadError(
                "provide_tool expects a ToolSpec",
                key="provide_tool",
                context={"plugin_id": self.id, "got": type(spec).__name__},
            )
        if spec.name not in self._declared:
            raise PluginToolUndeclared(
                f"tool {spec.name!r} is not declared in plugin.yaml",
                key="capabilities.tools",
                context={"plugin_id": self.id, "tool": spec.name},
            )
        self._staging.add_tool(spec)

    # -- hooks / subscriptions ---------------------------------------------
    def on(self, pattern: str, handler: Callable[..., Any]) -> Callable[[], None]:
        """Stage a hook handler or subscription, only while ``setup`` runs.

        ``pattern`` is ``hook.<name>`` for one of ``capabilities.hooks`` or an
        exact event type from ``subscribes``; anything else is a namespace
        violation (§6.3).  The returned unsubscribe closure removes the staged
        entry and is idempotent; once the host seals the context it is a no-op
        (runtime unsubscription on unload is host-owned).
        """
        if not callable(handler):
            raise PluginLoadError(
                "on expects a callable handler",
                key="on",
                hint="pass a sync or async function",
                context={
                    "plugin_id": self.id,
                    "pattern": pattern,
                    "got": type(handler).__name__,
                },
            )
        if self._sealed:
            raise PluginLoadError(
                f"cannot register {pattern!r} after the plugin was sealed",
                key="on",
                hint="call ctx.on during setup, before the load commits",
                context={"plugin_id": self.id, "pattern": pattern},
            )

        if pattern.startswith("hook."):
            name = pattern[len("hook."):]
            if name not in self._declared_hooks:
                raise PluginNamespaceError(
                    f"hook {name!r} is not declared in plugin.yaml",
                    key="capabilities.hooks",
                    hint="declare it under capabilities.hooks",
                    context={"plugin_id": self.id, "hook": name},
                )
            entry = self._staging.add_hook(name, handler)

            def remove() -> None:
                self._staging._remove_hook(entry)
        else:
            if pattern not in self._declared_subscribes:
                raise PluginNamespaceError(
                    f"event type {pattern!r} is not declared in subscribes",
                    key="subscribes",
                    hint="declare it under subscribes in plugin.yaml",
                    context={"plugin_id": self.id, "type": pattern},
                )
            entry = self._staging.add_subscription(pattern, handler)

            def remove() -> None:
                self._staging._remove_subscription(entry)

        def unsubscribe() -> None:
            if not self._sealed:
                remove()

        return unsubscribe

    # -- publishing ---------------------------------------------------------
    async def publish(self, etype: str, payload: Mapping[str, Any]) -> bool:
        """Publish one declared event through the host runtime (§6.3).

        The runtime owns ``origin``/``seq``/``session_id``/``cause_id`` and the
        rate limit; it returns ``False`` when the event is shed.  Undeclared
        types and namespace escapes raise :class:`PluginNamespaceError`.
        """
        if not self._committed:
            raise PluginLoadError(
                "publish is only available after the plugin is committed",
                key="publish",
                hint="the host commits the plugin after setup returns",
                context={"plugin_id": self.id, "type": etype},
            )
        if self._runtime is None:
            raise PluginLoadError(
                "publish requires a host runtime",
                key="publish",
                hint="PluginContext was constructed without a runtime",
                context={"plugin_id": self.id, "type": etype},
            )
        if not isinstance(payload, Mapping):
            raise PluginLoadError(
                "publish payload must be a mapping",
                key="publish",
                hint="pass a JSON object, not a list or scalar",
                context={
                    "plugin_id": self.id,
                    "type": etype,
                    "got": type(payload).__name__,
                },
            )
        if not self._publish_allowed(etype):
            raise PluginNamespaceError(
                f"event type {etype!r} is not declared in publishes",
                key="publishes",
                hint="declare it under publishes in plugin.yaml",
                context={"plugin_id": self.id, "type": etype},
            )
        return await self._runtime.publish(etype, payload)

    def _publish_allowed(self, etype: str) -> bool:
        return matches_publish_patterns(etype, self._publish_patterns)

    def seal(self, committed: bool = True) -> None:
        """Host-only: close ``setup``; ``committed`` also enables ``publish``."""
        self._sealed = True
        if committed:
            self._committed = True

    # -- lifecycle ----------------------------------------------------------
    def on_unload(self, callback: Callable[[], Any]) -> None:
        """Register a cleanup callback (sync or async), called in reverse order."""
        if not callable(callback):
            raise PluginLoadError(
                "on_unload expects a callable", key="on_unload",
                context={"plugin_id": self.id},
            )
        self._staging.add_callback(callback)

    # -- logging ------------------------------------------------------------
    def log(self, level: str, message: str, **fields: object) -> None:
        """Write a structured host log record; never raises into the plugin."""
        try:
            if self._log_sink is None:
                return
            self._log_sink({
                "kind": "plugin",
                "level": level,
                "msg": message,
                "plugin_id": self.id,
                "ts": time.time(),
                **fields,
            })
        except Exception:  # noqa: BLE001 - logging must not break setup
            return


__all__ = ["PluginContext", "Staging", "matches_publish_patterns"]
