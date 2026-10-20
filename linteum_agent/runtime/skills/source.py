"""Where skill packages come from (§3.1 / §7.7 of the skills design).

The skills layer does **not** discover plugins itself.  The plugin layer is the
place that acquires and routes third-party content; it hands the skills layer a
sequence of :class:`SkillSource` implementations.  A plugin with only tools
contributes no ``SkillPackage``, so the skills layer stays completely unaware
of it.

``DirectorySkillSource`` is the v0.1 implementation: each configured root is a
*container* (one level of skill directories).  A future ``PluginSkillSource``
materialises plugin-provided skill directories and implements the same
protocol, so the catalog builder does not change.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .model import SkillPackage

#: Stable source labels (also rendered in the L0 index).
SOURCE_PLUGIN = "plugin"
SOURCE_EXTRA = "extra"
SOURCE_PROJECT = "project"
SOURCE_USER = "user"

#: v1 precedence.  Higher wins; project intentionally overrides user.
PRIORITY_PLUGIN = 400
PRIORITY_EXTRA = 300
PRIORITY_PROJECT = 200
PRIORITY_USER = 100


@dataclass(frozen=True, slots=True)
class DiscoveryResult:
    """Packages found by one source plus source-level warnings."""

    packages: tuple[SkillPackage, ...] = ()
    warnings: tuple[str, ...] = ()


class SkillSource(Protocol):
    """The seam the plugin layer implements (structural, no base class)."""

    source_id: str
    priority: int

    async def discover(self) -> DiscoveryResult: ...


@dataclass(frozen=True, slots=True)
class DirectorySkillSource:
    """Scan a container directory whose immediate children are skills.

    ``missing_ok=True`` is used for optional roots (the default user directory
    may not exist); ``missing_ok=False`` is used for explicitly configured
    ``extra_dirs``, where a missing path deserves a warning.
    """

    root: Path
    source_id: str
    priority: int
    missing_ok: bool = True

    async def discover(self) -> DiscoveryResult:
        root = self.root.expanduser()
        try:
            if not root.is_dir():
                warnings = () if self.missing_ok else (
                    f"configured skill root is not a directory: {root.name or 'root'}",
                )
                return DiscoveryResult((), warnings)
        except OSError as exc:
            return DiscoveryResult((), (
                f"skill root is unavailable: {type(exc).__name__}",
            ))

        try:
            children = sorted(root.iterdir(), key=lambda item: item.name)
        except OSError as exc:
            return DiscoveryResult((), (
                f"skill root cannot be listed: {type(exc).__name__}",
            ))

        packages: list[SkillPackage] = []
        warnings: list[str] = []
        for child in children:
            if child.name.startswith("."):
                continue
            try:
                if child.is_symlink():
                    # A symlinked skill directory could point outside the
                    # configured container; only real children are admitted.
                    warnings.append(f"skill directory is a symlink, skipped: {child.name}")
                    continue
                if not child.is_dir():
                    continue
            except OSError:
                warnings.append(f"skill directory is unreadable: {child.name}")
                continue
            skill_md = child / "SKILL.md"
            try:
                # A copied-in skill must not let a symlink point outside the
                # container; a regular SKILL.md is required.
                if not skill_md.is_file() or skill_md.is_symlink():
                    continue
            except OSError:
                warnings.append(f"SKILL.md is unreadable: {child.name}")
                continue
            packages.append(SkillPackage(self.source_id, self.priority, child))
        return DiscoveryResult(tuple(packages), tuple(warnings))


__all__ = [
    "DirectorySkillSource",
    "DiscoveryResult",
    "PRIORITY_EXTRA",
    "PRIORITY_PLUGIN",
    "PRIORITY_PROJECT",
    "PRIORITY_USER",
    "SOURCE_EXTRA",
    "SOURCE_PLUGIN",
    "SOURCE_PROJECT",
    "SOURCE_USER",
    "SkillSource",
]
