"""Immutable value objects for the mode layer (§6.3/§7.7/§9.5).

The mode layer is a leaf: these objects carry parsed definitions, catalog
entries, render results and the JSON snapshot, but never a live registry,
bus, socket or host.  ``ToolSpec`` still owns a callable (the tool runtime's
only handle), so ``ModeResolution`` must never cross the event bus.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional, Protocol, runtime_checkable

from ..skills.model import SkillCatalog
from ..tools.spec import ToolSpec
from .errors import ModeSnapshotInvalid


@dataclass(frozen=True, slots=True)
class ToolSelection:
    """``tools.include`` / ``tools.exclude`` in a mode definition."""

    include: Optional[tuple[str, ...]] = None
    exclude: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SkillSelection:
    """``skills.enabled`` / ``skills.include`` / ``skills.exclude``."""

    enabled: bool = True
    include: Optional[tuple[str, ...]] = None
    exclude: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ModeDefinition:
    """One parsed mode, before resolution against host capabilities."""

    name: str
    description: str = ""
    system_prompt: str = ""
    extends: Optional[str] = None
    prompt_mode: str = "append"
    tools: ToolSelection = field(default_factory=ToolSelection)
    skills: SkillSelection = field(default_factory=SkillSelection)
    source_id: str = "builtin"
    priority: int = 0
    path: Optional[Path] = None
    extra: Mapping[str, Any] = field(default_factory=dict)
    #: Internal inheritance markers: ``True`` when the document contained the
    #: section.  A missing section inherits the parent's effective selection.
    tools_provided: bool = False
    skills_provided: bool = False


@dataclass(frozen=True, slots=True)
class ModePackage:
    """One discovered mode file (or a builtin marker with no file)."""

    name: str
    path: Optional[Path]
    source_id: str
    priority: int
    builtin: bool = False


@dataclass(frozen=True, slots=True)
class ModeDiscovery:
    """A source's discovery result."""

    packages: tuple[ModePackage, ...] = ()
    warnings: tuple[str, ...] = ()


@runtime_checkable
class ModeSource(Protocol):
    """Discovery seam for builtin and plugin mode packages (spec §6.1)."""

    source_id: str
    priority: int

    async def discover(self) -> ModeDiscovery: ...


@dataclass(frozen=True, slots=True)
class ModeIssue:
    """One invalid mode entry (catalog/list/validate), not an exception."""

    name: str
    source_id: str
    path: Optional[Path]
    error: str
    code: str

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "source": self.source_id,
            "error": self.error,
            "code": self.code,
        }


@dataclass(frozen=True, slots=True)
class ModeEntry:
    """One admitted catalog entry (the winner for its name)."""

    definition: ModeDefinition
    source_id: str
    priority: int
    path: Optional[Path]
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ModeCatalog:
    """The immutable, host-side mode catalog."""

    entries: Mapping[str, ModeEntry]
    invalid: tuple[ModeIssue, ...] = ()
    warnings: tuple[str, ...] = ()
    digest: str = ""

    def get(self, name: str) -> Optional[ModeEntry]:
        return self.entries.get(name)

    def __len__(self) -> int:
        return len(self.entries)

    def __bool__(self) -> bool:
        return bool(self.entries)


@dataclass(frozen=True, slots=True)
class ModeLimits:
    """Runtime-side settings view; the host builds it from ``ModeSettings``."""

    max_mode_bytes: int = 64 * 1024
    max_modes: int = 128


@dataclass(frozen=True, slots=True)
class ModeSnapshot:
    """A JSON-serialisable session snapshot (§9.5/§16.3)."""

    schema: int
    name: str
    description: str
    source_id: str
    extends: Optional[str]
    prompt_mode: str
    system_prompt: str
    environment_block: str
    tools_include: Optional[tuple[str, ...]]
    tools_exclude: tuple[str, ...]
    skills_enabled: bool
    skills_include: Optional[tuple[str, ...]]
    skills_exclude: tuple[str, ...]
    resolved_tools: tuple[str, ...]
    subagent: bool

    def to_dict(self) -> dict:
        return {
            "schema": self.schema,
            "name": self.name,
            "description": self.description,
            "source_id": self.source_id,
            "extends": self.extends,
            "prompt_mode": self.prompt_mode,
            "system_prompt": self.system_prompt,
            "environment_block": self.environment_block,
            "tools_include": list(self.tools_include)
            if self.tools_include is not None else None,
            "tools_exclude": list(self.tools_exclude),
            "skills_enabled": self.skills_enabled,
            "skills_include": list(self.skills_include)
            if self.skills_include is not None else None,
            "skills_exclude": list(self.skills_exclude),
            "resolved_tools": list(self.resolved_tools),
            "subagent": self.subagent,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ModeSnapshot":
        schema = int(data.get("schema", 1))
        if schema != 1:
            raise ModeSnapshotInvalid(
                f"unsupported mode snapshot schema: {schema}",
                key="mode_snapshot",
                context={"schema": schema},
            )
        tools_include = data.get("tools_include")
        skills_include = data.get("skills_include")
        try:
            return cls(
                schema=schema,
                name=str(data["name"]),
                description=str(data.get("description", "")),
                source_id=str(data["source_id"]),
                extends=(
                    str(data["extends"])
                    if data.get("extends") is not None else None
                ),
                prompt_mode=str(data.get("prompt_mode", "append")),
                system_prompt=str(data.get("system_prompt", "")),
                environment_block=str(data.get("environment_block", "")),
                tools_include=(
                    tuple(str(item) for item in tools_include)
                    if tools_include is not None else None
                ),
                tools_exclude=tuple(
                    str(item) for item in (data.get("tools_exclude") or ())
                ),
                skills_enabled=bool(data.get("skills_enabled", True)),
                skills_include=(
                    tuple(str(item) for item in skills_include)
                    if skills_include is not None else None
                ),
                skills_exclude=tuple(
                    str(item) for item in (data.get("skills_exclude") or ())
                ),
                resolved_tools=tuple(
                    str(item) for item in (data.get("resolved_tools") or ())
                ),
                subagent=bool(data.get("subagent", False)),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ModeSnapshotInvalid(
                f"invalid mode snapshot: {exc}",
                key="mode_snapshot",
            ) from exc


@dataclass(frozen=True, slots=True)
class ModeSummary:
    """Small, JSON-able view exposed to Gateway/CLI/TUI."""

    name: str
    description: str
    source_id: str
    builtin: bool = False
    default: bool = False
    digest: str = ""

    @property
    def id(self) -> str:
        """Front-end display id: ``<source_id>/<name>`` (§13.2)."""
        return f"{self.source_id}/{self.name}"


@dataclass(frozen=True, slots=True)
class ModeResolution:
    """Pure resolver output: no registry, no host I/O (§7.7)."""

    name: str
    description: str
    source_id: str
    digest: str
    snapshot: ModeSnapshot
    system_prompt: str
    environment_block: str
    tool_specs: tuple[ToolSpec, ...]
    subagent_tools: frozenset[str]
    filtered_skills: SkillCatalog
    tool_names: tuple[str, ...]
    skill_names: tuple[str, ...]
    warnings: tuple[str, ...] = ()


__all__ = [
    "ModeCatalog",
    "ModeDefinition",
    "ModeDiscovery",
    "ModeEntry",
    "ModeIssue",
    "ModeLimits",
    "ModePackage",
    "ModeResolution",
    "ModeSnapshot",
    "ModeSource",
    "ModeSummary",
    "SkillSelection",
    "ToolSelection",
]
