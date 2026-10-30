"""``PluginContext`` — the only host surface a capability plugin may use (§5.2).

The context is a deliberately small facade.  It exposes **no** bus, registry,
``AgentState`` or host reference, so plugin code cannot publish events, read a
live tool table or mutate session state.  Its only state-changing action is
``provide_tool``, which writes to a per-plugin staging area; the host commits
that staging atomically *after* ``setup`` returns (two-phase load, §8.4).
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from ..tools.spec import ToolSpec
from .errors import PluginConflict, PluginLoadError, PluginToolUndeclared
from .model import PluginManifest


@dataclass
class Staging:
    """Per-load staging area: tools and unload callbacks, discarded on failure.

    ``provide_tool`` may be called from a worker thread (synchronous ``setup``
    runs through ``asyncio.to_thread``), so the lists are guarded by a plain
    :class:`threading.Lock` rather than an asyncio lock.
    """

    plugin_id: str
    tools: list[ToolSpec] = field(default_factory=list)
    callbacks: list[Callable[[], Any]] = field(default_factory=list)
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

    def snapshot(self) -> tuple[tuple[ToolSpec, ...], tuple[Callable[[], Any], ...]]:
        with self._lock:
            return tuple(self.tools), tuple(self.callbacks)


class PluginContext:
    """The host facade handed to ``setup(ctx)``.

    ``id``/``version``/``plugin_dir`` are read-only facts.  ``provide_tool``
    fails immediately for a tool the manifest did not declare, which makes a
    typo a load failure instead of a silently missing capability.
    """

    def __init__(
        self,
        manifest: PluginManifest,
        staging: Staging,
        *,
        log: Optional[Callable[[dict], None]] = None,
    ) -> None:
        self.id = manifest.id
        self.version = manifest.version
        self.plugin_dir: Path = manifest.root
        self._manifest = manifest
        self._staging = staging
        self._declared = frozenset(item.name for item in manifest.tools)
        self._log_sink = log

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


__all__ = ["PluginContext", "Staging"]
