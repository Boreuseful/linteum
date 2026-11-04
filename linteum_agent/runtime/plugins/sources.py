"""Skill/mode discovery seams backed by the plugin host (§6.3 / §7.2).

The skills and modes layers deliberately know nothing about plugin manifests.
They consume the same ``SkillSource`` / ``ModeSource`` protocols as before; the
plugin host materialises packages with ``source_id="plugin:<id>"`` and the
fixed priority 400 (D14).

``PluginModeSource`` already exists in :mod:`linteum_agent.runtime.modes.source`
and only needs an object exposing ``mode_packages()`` — the host implements that
method, so this module re-exports the adapter rather than defining a second
protocol.
"""

from __future__ import annotations

from ..modes.source import PluginModeProvider, PluginModeSource
from ..skills.source import (
    PRIORITY_PLUGIN,
    SOURCE_PLUGIN,
    DiscoveryResult,
)


class PluginSkillSource:
    """``SkillSource`` over the plugin host's active packages.

    Existence is re-checked on every discovery.  A plugin that removed a skill
    directory after loading yields a ``plugin.skill_missing`` warning instead of
    an exception; already-bound sessions keep their in-memory snapshot.
    """

    source_id = SOURCE_PLUGIN
    priority = PRIORITY_PLUGIN

    def __init__(self, host) -> None:
        self._host = host

    async def discover(self) -> DiscoveryResult:
        packages, warnings = self._host.skill_packages_with_warnings()
        return DiscoveryResult(tuple(packages), tuple(warnings))


__all__ = [
    "PluginModeProvider",
    "PluginModeSource",
    "PluginSkillSource",
]
