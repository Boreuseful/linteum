"""Session-scoped mode definitions, catalog and resolution (§16.1).

A mode selects a system-prompt body, a subset of the host's tools and a
visible subset of the session's skills.  The package is a leaf: it parses
plugin-delivered Markdown files, resolves selectors against host-provided
capability lists and renders prompt blocks.  All I/O and lifecycle work lives
in :class:`linteum_agent.runtime.host.AgentHost`.
"""

from __future__ import annotations

from .catalog import load_mode_catalog
from .errors import (
    ModeBusy,
    ModeError,
    ModeExtends,
    ModeImmutable,
    ModeInvalid,
    ModeLimit,
    ModeNotFound,
    ModeReserved,
    ModeSnapshotInvalid,
    ModeSnapshotMissing,
    ModeTemplate,
)
from .model import (
    ModeCatalog,
    ModeDefinition,
    ModeDiscovery,
    ModeEntry,
    ModeIssue,
    ModeLimits,
    ModePackage,
    ModeResolution,
    ModeSnapshot,
    ModeSource,
    ModeSummary,
    SkillSelection,
    ToolSelection,
)
from .selection import resolve_mode
from .source import BuiltinModeSource, PluginModeProvider, PluginModeSource

__all__ = [
    "BuiltinModeSource",
    "ModeBusy",
    "ModeCatalog",
    "ModeDefinition",
    "ModeDiscovery",
    "ModeEntry",
    "ModeError",
    "ModeExtends",
    "ModeImmutable",
    "ModeInvalid",
    "ModeIssue",
    "ModeLimit",
    "ModeLimits",
    "ModeNotFound",
    "ModePackage",
    "ModeReserved",
    "ModeResolution",
    "ModeSnapshot",
    "ModeSnapshotInvalid",
    "ModeSnapshotMissing",
    "ModeSource",
    "ModeSummary",
    "ModeTemplate",
    "PluginModeProvider",
    "PluginModeSource",
    "SkillSelection",
    "ToolSelection",
    "load_mode_catalog",
    "resolve_mode",
]
