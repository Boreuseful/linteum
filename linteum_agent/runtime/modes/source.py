"""Mode discovery sources (§6.1).

v0.1 has exactly two sources:

* :class:`BuiltinModeSource` returns the code-defined ``standard``/``code``
  packages (no filesystem access).
* :class:`PluginModeSource` is the seam for an enabled plugin host.  The
  plugin layer materialises ``capabilities.modes`` files and hands the
  resulting packages to this adapter.  There is deliberately no directory
  source: modes never load from user or workspace paths.
"""

from __future__ import annotations

import inspect
from typing import Awaitable, Protocol, Sequence, Union

from .builtin import BUILTIN_MODES
from .model import ModeDiscovery, ModePackage


class BuiltinModeSource:
    """The two code-defined modes; no I/O."""

    source_id = "builtin"
    priority = 0

    async def discover(self) -> ModeDiscovery:
        return ModeDiscovery(
            packages=tuple(
                ModePackage(
                    name=name,
                    path=None,
                    source_id=self.source_id,
                    priority=self.priority,
                    builtin=True,
                )
                for name in sorted(BUILTIN_MODES)
            )
        )


class PluginModeProvider(Protocol):
    """What :class:`PluginModeSource` needs from the future plugin host."""

    def mode_packages(self) -> (
        Sequence[ModePackage] | Awaitable[Sequence[ModePackage]]
    ): ...


class PluginModeSource:
    """Discover packages materialised by enabled plugins (seam only in v0.1)."""

    source_id = "plugin"
    priority = 400

    def __init__(self, provider: PluginModeProvider) -> None:
        self._provider = provider

    async def discover(self) -> ModeDiscovery:
        result: Union[
            Sequence[ModePackage], Awaitable[Sequence[ModePackage]]
        ]
        if hasattr(self._provider, "mode_packages"):
            result = self._provider.mode_packages()
        else:  # pragma: no cover - convenience for tiny fake providers
            result = self._provider()  # type: ignore[operator]
        if inspect.isawaitable(result):
            result = await result
        return ModeDiscovery(packages=tuple(result))


__all__ = [
    "BuiltinModeSource",
    "PluginModeProvider",
    "PluginModeSource",
]
