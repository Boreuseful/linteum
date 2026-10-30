"""Immutable value objects for the plugin layer (§3 / §4 / §11.1).

A plugin manifest is a *validated* description of a directory: the Python entry
point, the declared tool security metadata, and the materialised skill/mode
paths.  ``PluginManifest`` never holds a callable or a module; those live in the
host's ``LoadedPlugin`` and are never serialised.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

#: Plugin id / source-id suffix (§4.3).
PLUGIN_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
#: Plugin version (display/diagnostics only).
PLUGIN_VERSION_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.+_-]{0,63}$")
#: Declared tool names; ``mcp__`` and ``:`` are reserved for other namespaces.
TOOL_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
#: ``relative/file.py:callable`` (dotted attributes are allowed).
ENTRY_PATTERN = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$"
)

#: Accepted manifest ``runtime`` values in v0.1.
RUNTIME_INPROC = "in-proc"
#: Accepted ``trust`` values (metadata only).
TRUST_VALUES: tuple[str, ...] = ("official", "third-party", "local")
#: Accepted declared tool backends in v0.1.
TOOL_BACKENDS: tuple[str, ...] = ("inproc", "thread")
#: Accepted tool side effects.
SIDE_EFFECTS: tuple[str, ...] = ("pure", "read", "write", "dangerous")

#: Lifecycle states (§8.2).
STATE_DISCOVERED = "discovered"
STATE_DISABLED = "disabled"
STATE_INVALID = "invalid"
STATE_LOADING = "loading"
STATE_ACTIVE = "active"
STATE_FAILED = "failed"
STATE_QUIESCING = "quiescing"
STATE_UNLOADED = "unloaded"

#: States whose capabilities are visible to new catalogs/sessions.
VISIBLE_STATES: frozenset[str] = frozenset({STATE_ACTIVE})


@dataclass(frozen=True, slots=True)
class PluginEntry:
    """A validated ``entry`` declaration: the Python file and its setup callable."""

    path: Path
    qualname: str


@dataclass(frozen=True, slots=True)
class ToolDeclaration:
    """Security metadata from ``capabilities.tools`` (§4.4).

    The declaration is the operator-auditable contract.  The host takes the
    stricter of declaration and runtime ``ToolSpec`` (§5.3).
    """

    name: str
    side_effect: str
    timeout: float = 30.0
    concurrency: int = 4
    idempotent: bool = True
    requires_approval: bool = False
    backend: str = "inproc"


@dataclass(frozen=True, slots=True)
class PluginManifest:
    """One validated ``plugin.yaml`` plus its resolved declared paths.

    ``source`` is the container-relative directory name (never an absolute
    path).  ``digest`` hashes the normalised manifest bytes only; the host adds
    the actual tool names when it records a loaded plugin (§14.3).
    """

    id: str
    version: str
    root: Path
    source: str
    description: str = ""
    entry: Optional[PluginEntry] = None
    runtime: str = RUNTIME_INPROC
    trust: str = "third-party"
    api_version: int = 1
    tools: tuple[ToolDeclaration, ...] = ()
    skill_dirs: tuple[Path, ...] = ()
    mode_paths: tuple[Path, ...] = ()
    digest: str = ""
    warnings: tuple[str, ...] = field(default_factory=tuple)

    @property
    def tool_names(self) -> tuple[str, ...]:
        return tuple(item.name for item in self.tools)

    @property
    def skill_names(self) -> tuple[str, ...]:
        return tuple(path.name for path in self.skill_dirs)

    @property
    def mode_names(self) -> tuple[str, ...]:
        return tuple(path.stem for path in self.mode_paths)

    @property
    def has_capabilities(self) -> bool:
        return bool(self.tools or self.skill_dirs or self.mode_paths)


@dataclass(frozen=True, slots=True)
class PluginLimits:
    """Resource limits threaded from ``agent.plugins`` (§8.3 / §12.5)."""

    max_plugins: int = 64
    max_tools_per_plugin: int = 64
    max_manifest_bytes: int = 64 * 1024
    load_timeout_ms: int = 10_000
    drain_grace_ms: int = 5_000
    unload_timeout_ms: int = 5_000


@dataclass(frozen=True, slots=True)
class PluginIssue:
    """One directory that could not be admitted during discovery.

    ``id`` is filled when the manifest is parseable enough to identify the
    plugin, which lets a fixed directory be reloaded without a restart (§8.4).
    """

    source: str
    code: str
    error: str
    hint: str = ""
    id: str = ""
    root: Optional[Path] = None

    def to_dict(self) -> dict:
        return {"source": self.source, "code": self.code, "error": self.error}


@dataclass(frozen=True, slots=True)
class PluginCatalog:
    """The immutable result of one discovery scan."""

    manifests: tuple[PluginManifest, ...] = ()
    invalid: tuple[PluginIssue, ...] = ()
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PluginSummary:
    """Small, JSON-able view exposed to ``plugin.list`` / host callers."""

    id: str
    version: str
    description: str
    state: str
    trust: str
    source: str
    tools: tuple[str, ...] = ()
    skills: tuple[str, ...] = ()
    modes: tuple[str, ...] = ()
    digest: str = ""
    error: Optional[dict] = None
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "version": self.version,
            "state": self.state,
            "trust": self.trust,
            "description": self.description,
            "source": self.source,
            "tools": list(self.tools),
            "skills": list(self.skills),
            "modes": list(self.modes),
            "digest": self.digest,
            "error": self.error,
            "warnings": list(self.warnings),
        }


__all__ = [
    "ENTRY_PATTERN",
    "PLUGIN_ID_PATTERN",
    "PLUGIN_VERSION_PATTERN",
    "PluginCatalog",
    "PluginEntry",
    "PluginIssue",
    "PluginLimits",
    "PluginManifest",
    "PluginSummary",
    "RUNTIME_INPROC",
    "SIDE_EFFECTS",
    "STATE_ACTIVE",
    "STATE_DISABLED",
    "STATE_DISCOVERED",
    "STATE_FAILED",
    "STATE_INVALID",
    "STATE_LOADING",
    "STATE_QUIESCING",
    "STATE_UNLOADED",
    "TOOL_BACKENDS",
    "TOOL_NAME_PATTERN",
    "TRUST_VALUES",
    "VISIBLE_STATES",
    "ToolDeclaration",
]
