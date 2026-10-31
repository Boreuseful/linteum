"""Third-party capability plugins (§25–28 of the event design).

A plugin is a directory containing ``plugin.yaml``.  v0.1 supports three
capabilities — tools, skills and modes — and executes tool plugins **in the
host process** (trusted by installation + explicit allowlist).  Content-only
plugins never execute code.

Public entry points:

* :class:`PluginHost` — discovery, two-phase load, quiesce/drain unload and the
  ``system.plugin.*`` lifecycle events;
* :class:`PluginContext` — the only facade handed to a plugin's ``setup``;
* :class:`PluginSkillSource` / :class:`PluginModeSource` — the skill/mode source
  adapters consumed by the existing catalogs;
* value objects (:class:`PluginManifest`, :class:`PluginSummary`, …) and the
  stable :class:`PluginError` hierarchy.

The package is host-side only: nothing here enters ``AgentState``, a checkpoint
or an event payload.  Replay never imports a plugin (§14.1).
"""

from __future__ import annotations

from .context import PluginContext, Staging
from .errors import (
    PluginBusy,
    PluginConfigError,
    PluginConflict,
    PluginDisabled,
    PluginError,
    PluginManageDisabled,
    PluginLimit,
    PluginLoadError,
    PluginManifestError,
    PluginNotFound,
    PluginPathError,
    PluginTimeout,
    PluginToolMissing,
    PluginToolUndeclared,
    PluginUnsupported,
    as_plugin_error,
)
from .host import LoadedPlugin, PluginHost
from .loader import MAX_ABANDONED_LOADERS, abandoned_loaders
from .manifest import MANIFEST_NAME, read_plugin_manifest, scan_plugins
from .model import (
    PLUGIN_ID_PATTERN,
    PLUGIN_VERSION_PATTERN,
    PluginCatalog,
    PluginEntry,
    PluginIssue,
    PluginLimits,
    PluginManifest,
    PluginSummary,
    ToolDeclaration,
)
from .sources import PluginModeSource, PluginSkillSource

__all__ = [
    "MANIFEST_NAME",
    "MAX_ABANDONED_LOADERS",
    "PLUGIN_ID_PATTERN",
    "PLUGIN_VERSION_PATTERN",
    "LoadedPlugin",
    "PluginBusy",
    "PluginCatalog",
    "PluginConfigError",
    "PluginConflict",
    "PluginContext",
    "PluginDisabled",
    "PluginEntry",
    "PluginError",
    "PluginHost",
    "PluginManageDisabled",
    "PluginIssue",
    "PluginLimit",
    "PluginLimits",
    "PluginLoadError",
    "PluginManifest",
    "PluginManifestError",
    "PluginModeSource",
    "PluginNotFound",
    "PluginPathError",
    "PluginSkillSource",
    "PluginSummary",
    "PluginTimeout",
    "PluginToolMissing",
    "PluginToolUndeclared",
    "PluginUnsupported",
    "Staging",
    "ToolDeclaration",
    "abandoned_loaders",
    "as_plugin_error",
    "read_plugin_manifest",
    "scan_plugins",
]
