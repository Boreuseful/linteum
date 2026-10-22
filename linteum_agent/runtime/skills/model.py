"""Immutable value objects for the skills layer (§5–§7 of the skills design).

A :class:`SkillCatalog` is a **session-scoped in-memory snapshot**: parsing
reads `SKILL.md` and every readable text resource once, hashes them, and the
two loader tools then serve content without touching the filesystem.  Nothing
here is JSON-serialisable state belonging to the Agent; the catalog is host
input, and only its rendered index text enters ``ContextPlan.messages``.

The dataclasses are frozen and slots-based so a catalog can be shared by the
session's tool registry and context builder without accidental mutation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

#: Valid skill names (also used by the frontmatter parser).
SKILL_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")

#: Resource kinds accepted by the v0.1 design.
RESOURCE_KINDS: tuple = ("reference", "template", "script", "asset")

#: Conventional resource directories (auto-discovered).
RESOURCE_DIRS: tuple = ("references", "templates", "scripts", "assets")


@dataclass(frozen=True, slots=True)
class SkillPackage:
    """A materialised skill directory handed to the skills layer by a source.

    ``source_id`` is a stable label (``user`` / ``project`` / ``extra`` /
    ``plugin:<id>``); ``priority`` is the catalog precedence.  ``root`` must
    contain a ``SKILL.md``.
    """

    source_id: str
    priority: int
    root: Path
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SkillResource:
    """One on-demand resource inside a skill.

    ``content`` is populated only for ``readable`` text resources; binary
    ``asset`` rows (and resources above ``max_resource_bytes``) carry metadata
    only.  ``sha256`` is ``None`` when the file was not read.
    """

    path: str
    kind: str
    description: str
    size: int
    sha256: Optional[str]
    readable: bool
    content: Optional[str] = None

    def __post_init__(self) -> None:
        if self.kind not in RESOURCE_KINDS:
            raise ValueError(f"unknown resource kind: {self.kind!r}")


@dataclass(frozen=True, slots=True)
class SkillEntry:
    """One admitted skill in the session catalog."""

    name: str
    description: str
    when_to_use: Optional[str]
    version: str
    source_id: str
    priority: int
    body: str
    body_sha256: str
    body_size: int
    resources: tuple[SkillResource, ...] = ()


@dataclass(frozen=True, slots=True)
class SkillLimits:
    """Frozen per-session limits used by the loader tools and the parser.

    ``max_result_bytes`` must stay below ``ToolRuntime``'s inline threshold so
    a tool result is never externalised into an ``art:`` placeholder.
    """

    max_result_bytes: int = 7 * 1024
    max_skill_bytes: int = 256 * 1024
    max_resource_bytes: int = 1024 * 1024
    max_resources_per_skill: int = 64
    max_index_bytes: int = 8 * 1024
    max_skills: int = 256
    max_catalog_bytes: int = 16 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class SkillCatalog:
    """An immutable, session-scoped snapshot plus its rendered L0 index."""

    entries: tuple[SkillEntry, ...]
    index_text: str
    digest: str
    limits: SkillLimits
    warnings: tuple[str, ...] = field(default_factory=tuple)

    def get(self, name: str) -> Optional[SkillEntry]:
        """Return the entry named ``name``, or ``None``."""
        for entry in self.entries:
            if entry.name == name:
                return entry
        return None

    def __len__(self) -> int:
        return len(self.entries)

    def __bool__(self) -> bool:
        return bool(self.entries)


__all__ = [
    "RESOURCE_DIRS",
    "RESOURCE_KINDS",
    "SKILL_NAME_PATTERN",
    "SkillCatalog",
    "SkillEntry",
    "SkillLimits",
    "SkillPackage",
    "SkillResource",
]
