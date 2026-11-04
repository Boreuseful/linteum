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
    PluginNamespaceError,
    PluginNotFound,
    PluginPathError,
    PluginQuarantined,
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
    HOOK_NAMES,
    PLUGIN_ID_PATTERN,
    PLUGIN_VERSION_PATTERN,
    PluginAllowResult,
    PluginCatalog,
    PluginChange,
    PluginEntry,
    PluginEvent,
    PluginIssue,
    PluginLimits,
    PluginManifest,
    PluginOrdering,
    PluginRuntimeLimits,
    PluginSummary,
    RESERVED_HOOK_NAMES,
    STATE_QUARANTINED,
    SUBSCRIBABLE_TYPES,
    ToolDeclaration,
)
from .sources import PluginModeSource, PluginSkillSource

__all__ = [
    "HOOK_NAMES",
    "MANIFEST_NAME",
    "MAX_ABANDONED_LOADERS",
    "PLUGIN_ID_PATTERN",
    "PLUGIN_VERSION_PATTERN",
    "RESERVED_HOOK_NAMES",
    "STATE_QUARANTINED",
    "SUBSCRIBABLE_TYPES",
    "LoadedPlugin",
    "PluginAllowResult",
    "PluginBusy",
    "PluginCatalog",
    "PluginChange",
    "PluginConfigError",
    "PluginConflict",
    "PluginContext",
    "PluginDisabled",
    "PluginEntry",
    "PluginError",
    "PluginEvent",
    "PluginHost",
    "PluginManageDisabled",
    "PluginIssue",
    "PluginLimit",
    "PluginLimits",
    "PluginLoadError",
    "PluginManifest",
    "PluginManifestError",
    "PluginModeSource",
    "PluginNamespaceError",
    "PluginNotFound",
    "PluginOrdering",
    "PluginPathError",
    "PluginQuarantined",
    "PluginRuntimeLimits",
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
