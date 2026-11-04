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
from typing import Any, Mapping, Optional

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

#: Read-only observation hooks accepted in v0.4 (§6.1).
HOOK_NAMES: tuple[str, ...] = ("before_llm_request", "after_llm_completed")
#: Hook names reserved for a later, request-mutating API (§1.2).
RESERVED_HOOK_NAMES: tuple[str, ...] = (
    "tool_result_filter", "approval_policy", "plan_updated",
)
#: Event types a plugin may subscribe to exactly, with no wildcards (§6.2).
SUBSCRIBABLE_TYPES: frozenset[str] = frozenset({
    "llm.completed",
    "llm.failed",
    "tool.call.completed",
    "tool.call.failed",
    "agent.completed",
    "agent.failed",
})

#: Lifecycle states (§8.2 / §6.7).
STATE_DISCOVERED = "discovered"
STATE_DISABLED = "disabled"
STATE_INVALID = "invalid"
STATE_LOADING = "loading"
STATE_ACTIVE = "active"
STATE_FAILED = "failed"
STATE_QUARANTINED = "quarantined"
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
class PluginRuntimeLimits:
    """Per-plugin quotas for hooks, subscriptions and publishing (§6.1 / §6.7)."""

    max_events_per_sec: int = 100
    max_handler_ms: int = 2_000
    max_concurrency: int = 4
    max_output_bytes: int = 65_536
    max_consecutive_failures: int = 5


@dataclass(frozen=True, slots=True)
class PluginOrdering:
    """Deterministic hook ordering and failure policy (§6.1 / §6.5)."""

    priority: int = 100
    fail_mode: str = "closed"


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
    config_schema: Optional[dict] = None
    needs_secrets: tuple[str, ...] = ()
    hooks: tuple[str, ...] = ()
    subscribes: tuple[str, ...] = ()
    publishes: tuple[str, ...] = ()
    runtime_limits: PluginRuntimeLimits = field(default_factory=PluginRuntimeLimits)
    ordering: PluginOrdering = field(default_factory=PluginOrdering)

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
        return bool(
            self.tools or self.skill_dirs or self.mode_paths
            or self.hooks or self.subscribes or self.publishes
        )


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
class PluginEvent:
    """Frozen, read-only event view handed to plugin handlers (§6.4).

    The plugin never receives the bus ``Event`` itself: ``origin``/``seq`` are
    host-stamped and the payload is the JSON body only.
    """

    type: str
    session_id: str
    payload: Mapping[str, Any]
    seq: Optional[int] = None
    origin: str = "core"
    ts: float = 0.0
    cause_id: Optional[str] = None
    trace_id: Optional[str] = None


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
    hooks: tuple[str, ...] = ()
    subscribes: tuple[str, ...] = ()
    publishes: tuple[str, ...] = ()
    #: ``key -> "default" | "file"``; values are never exposed (§4.5).
    config: tuple[tuple[str, str], ...] = ()
    #: Declared secret names only, never their values (§4.5).
    secrets: tuple[str, ...] = ()

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
            "hooks": list(self.hooks),
            "subscribes": list(self.subscribes),
            "publishes": list(self.publishes),
            "config": dict(self.config),
            "secrets": list(self.secrets),
        }


@dataclass(frozen=True, slots=True)
class PluginChange:
    """One per-id outcome of a runtime allow change (§5.2)."""

    id: str
    action: str
    state: str
    error: Optional[dict] = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "action": self.action,
            "state": self.state,
            "error": self.error,
        }


@dataclass(frozen=True, slots=True)
class PluginAllowResult:
    """JSON-able result of ``PluginHost.set_allowed`` (§5.2)."""

    allow: tuple[str, ...]
    mode: str
    changed: bool
    restart_required: bool
    changes: tuple[PluginChange, ...] = ()
    summaries: tuple[PluginSummary, ...] = ()

    def to_dict(self) -> dict:
        return {
            "allow": list(self.allow),
            "mode": self.mode,
            "changed": self.changed,
            "restart_required": self.restart_required,
            "changes": [change.to_dict() for change in self.changes],
            "summaries": [summary.to_dict() for summary in self.summaries],
        }


__all__ = [
    "ENTRY_PATTERN",
    "HOOK_NAMES",
    "PLUGIN_ID_PATTERN",
    "PLUGIN_VERSION_PATTERN",
    "PluginAllowResult",
    "PluginCatalog",
    "PluginChange",
    "PluginEntry",
    "PluginEvent",
    "PluginIssue",
    "PluginLimits",
    "PluginManifest",
    "PluginOrdering",
    "PluginRuntimeLimits",
    "PluginSummary",
    "RESERVED_HOOK_NAMES",
    "RUNTIME_INPROC",
    "SIDE_EFFECTS",
    "STATE_ACTIVE",
    "STATE_DISABLED",
    "STATE_DISCOVERED",
    "STATE_FAILED",
    "STATE_INVALID",
    "STATE_LOADING",
    "STATE_QUARANTINED",
    "STATE_QUIESCING",
    "STATE_UNLOADED",
    "SUBSCRIBABLE_TYPES",
    "TOOL_BACKENDS",
    "TOOL_NAME_PATTERN",
    "TRUST_VALUES",
    "VISIBLE_STATES",
    "ToolDeclaration",
]
