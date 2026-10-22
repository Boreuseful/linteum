"""Load one materialised skill package into an immutable :class:`SkillEntry`.

This is the only module in the skills layer that reads skill files.  It parses
the YAML frontmatter, discovers and merges resources, applies the size/budget
limits, hashes the admitted content and returns an in-memory entry.  The
catalog builder and the loader tools never touch the filesystem afterwards.

The parsing policy is deliberately **fail-soft at the skill boundary**: an
invalid skill returns ``(None, warnings)`` instead of raising, so one broken
third-party package cannot prevent a session from opening.  Errors that *are*
raised (for example an invalid declared resource path) are likewise converted
to a warning; only programming errors escape.
"""

from __future__ import annotations

import hashlib
import os
import re
import unicodedata
from pathlib import Path, PurePosixPath
from typing import Any, Optional

import yaml

from .model import (
    RESOURCE_DIRS,
    RESOURCE_KINDS,
    SKILL_NAME_PATTERN,
    SkillEntry,
    SkillLimits,
    SkillPackage,
    SkillResource,
)

#: Conventional directory -> resource kind.
_KIND_BY_DIR: dict[str, str] = {
    "references": "reference",
    "templates": "template",
    "scripts": "script",
    "assets": "asset",
}

#: Versions are displayed in the tool header; keep them to a safe token set.
VERSION_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")


def sanitise_version(value: object) -> str:
    """Return a header-safe version token (``"0"`` when invalid)."""
    text = normalise_display_text(str(value), limit=64) if value is not None else "0"
    return text if VERSION_PATTERN.match(text) else "0"


def normalise_display_text(value: str, *, limit: int = 1024) -> str:
    """Collapse untrusted frontmatter into one safe display line.

    Newlines/tabs become spaces, control characters are removed, runs of
    whitespace collapse, and the result is truncated.  The L0 index is a system
    message, so this is the boundary that prevents a description from forging
    extra index entries or system paragraphs.
    """
    if not isinstance(value, str):
        value = str(value)
    text = value.replace("\r", " ").replace("\n", " ").replace("\t", " ")
    text = "".join(
        ch for ch in text if unicodedata.category(ch) not in ("Cc", "Cf")
    )
    text = " ".join(text.split())
    return text[:limit]


def _parse_document(text: str) -> Optional[tuple[dict, str]]:
    """Split a ``SKILL.md`` document into ``(frontmatter, body)``.

    Returns ``None`` when the leading ``---`` block is missing, unterminated,
    not valid YAML, or not a mapping.
    """
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return None
    end = next(
        (index for index in range(1, len(lines)) if lines[index].strip() == "---"),
        None,
    )
    if end is None:
        return None
    try:
        data = yaml.safe_load("".join(lines[1:end])) or {}
    except Exception:  # noqa: BLE001 - malformed input must never escape fail-soft
        # PyYAML raises RecursionError (not YAMLError) on deeply nested input;
        # an untrusted SKILL.md must degrade to "skip this skill", not block a
        # session open.
        return None
    if not isinstance(data, dict):
        return None
    return data, "".join(lines[end + 1:])


def _inside(root: Path, candidate: Path) -> bool:
    """True when ``candidate`` resolves to a path inside ``root``."""
    try:
        resolved = candidate.resolve()
    except (OSError, RuntimeError):
        return False
    return resolved == root or str(resolved).startswith(str(root) + os.sep)


def _safe_relative(root: Path, candidate: Path) -> Optional[str]:
    """Return the POSIX relative path, or ``None`` for hidden/invalid paths."""
    try:
        relative = candidate.relative_to(root)
    except ValueError:
        return None
    parts = relative.parts
    if any(part in ("", ".", "..") or part.startswith(".") for part in parts):
        return None
    return PurePosixPath(*parts).as_posix()


def _normalise_declared_path(value: Any) -> Optional[str]:
    """Validate a frontmatter resource path (relative, POSIX, no traversal)."""
    if not isinstance(value, str):
        return None
    raw = value.strip()
    if not raw or "\x00" in raw or "\\" in raw or raw.startswith(("/", "~")):
        return None
    path = PurePosixPath(raw)
    if path.is_absolute():
        return None
    if any(
        part in ("", ".", "..") or part.startswith(".")
        for part in path.parts
    ):
        return None
    return path.as_posix()


def _discover_auto(root: Path, root_resolved: Path) -> tuple[dict[str, dict], list[str]]:
    """Scan the four conventional directories, skipping hidden/escaping paths."""
    found: dict[str, dict] = {}
    warnings: list[str] = []
    for dirname in RESOURCE_DIRS:
        base = root / dirname
        if not base.is_dir():
            continue
        try:
            candidates = sorted(base.rglob("*"), key=lambda item: item.as_posix())
        except OSError:
            warnings.append(f"resource scan failed: {dirname}")
            continue
        for candidate in candidates:
            if candidate.is_dir():
                continue
            relative = _safe_relative(root, candidate)
            if relative is None:
                warnings.append(f"resource skipped (hidden or invalid): {candidate.name}")
                continue
            if not _inside(root_resolved, candidate):
                warnings.append(f"resource skipped (escapes skill root): {relative}")
                continue
            found[relative] = {"kind": _KIND_BY_DIR[dirname], "description": ""}
    return found, warnings


def _declared_resources(data: dict) -> tuple[Optional[dict[str, dict]], list[str]]:
    """Parse the optional ``resources`` manifest; ``None`` means invalid skill."""
    raw = data.get("resources")
    if raw is None:
        return {}, []
    if not isinstance(raw, list):
        return None, ["resources must be a list"]
    declared: dict[str, dict] = {}
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            return None, [f"resources[{index}] must be a mapping"]
        path = _normalise_declared_path(item.get("path"))
        if path is None:
            return None, [f"resources[{index}].path is invalid"]
        if path in declared:
            return None, [f"resources[{index}] duplicates {path}"]
        kind = item.get("kind")
        if kind is not None and kind not in RESOURCE_KINDS:
            return None, [f"resources[{index}].kind is invalid"]
        description = item.get("description")
        declared[path] = {
            "kind": kind,
            "description": (
                normalise_display_text(description)
                if isinstance(description, str) else ""
            ),
        }
    return declared, []


def load_package(
    package: SkillPackage,
    limits: SkillLimits,
    *,
    budget_bytes: int,
) -> tuple[Optional[SkillEntry], tuple[str, ...]]:
    """Load one package, returning ``(entry, warnings)``.

    ``budget_bytes`` is the remaining ``max_catalog_bytes`` for the session.
    The check is deliberately conservative: the size of ``SKILL.md`` plus every
    candidate resource must fit *before* any content is read.
    """
    warnings = list(package.warnings)
    root = Path(package.root)
    try:
        root_resolved = root.resolve(strict=True)
    except (OSError, RuntimeError):
        return None, tuple(warnings + [f"skill root is unavailable: {root.name}"])
    if not root_resolved.is_dir():
        return None, tuple(warnings + [f"skill root is not a directory: {root.name}"])

    skill_md = root / "SKILL.md"
    if not skill_md.is_file() or skill_md.is_symlink():
        return None, tuple(warnings + [f"missing regular SKILL.md in {root.name!r}"])
    try:
        skill_size = skill_md.stat().st_size
    except OSError:
        return None, tuple(warnings + [f"cannot stat SKILL.md in {root.name!r}"])
    if skill_size > limits.max_skill_bytes:
        return None, tuple(warnings + [
            f"{root.name!r}: SKILL.md exceeds max_skill_bytes "
            f"({skill_size} > {limits.max_skill_bytes})"
        ])
    try:
        text = skill_md.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError):
        return None, tuple(warnings + [f"{root.name!r}: SKILL.md is not readable UTF-8"])

    parsed = _parse_document(text)
    if parsed is None:
        return None, tuple(warnings + [f"{root.name!r}: invalid YAML frontmatter"])
    data, body = parsed

    name = data.get("name")
    if not isinstance(name, str) or not SKILL_NAME_PATTERN.match(name):
        return None, tuple(warnings + [f"{root.name!r}: invalid skill name {name!r}"])
    description = data.get("description")
    if not isinstance(description, str) or not normalise_display_text(description):
        return None, tuple(warnings + [f"skill {name!r} is missing a description"])
    description = normalise_display_text(description)

    when_to_use = data.get("when_to_use")
    when_to_use = (
        normalise_display_text(when_to_use)
        if isinstance(when_to_use, str) and when_to_use.strip()
        else None
    )
    version = sanitise_version(data.get("version", "0"))

    auto, auto_warnings = _discover_auto(root, root_resolved)
    warnings.extend(auto_warnings)
    declared, declared_warnings = _declared_resources(data)
    if declared is None:
        return None, tuple(warnings + declared_warnings)
    warnings.extend(declared_warnings)

    candidates = dict(auto)
    for path, meta in declared.items():
        target = root / path
        if not _inside(root_resolved, target) or not target.is_file():
            return None, tuple(warnings + [
                f"declared resource is unavailable or escapes root: {path}"
            ])
        current = dict(candidates.get(path, {}))
        if meta.get("kind") is not None:
            current["kind"] = meta["kind"]
        if meta.get("description"):
            current["description"] = meta["description"]
        candidates[path] = current

    paths = sorted(candidates)
    if len(paths) > limits.max_resources_per_skill:
        omitted = len(paths) - limits.max_resources_per_skill
        warnings.append(
            f"skill {name!r}: {omitted} resources omitted "
            f"(max_resources_per_skill={limits.max_resources_per_skill})"
        )
        paths = paths[: limits.max_resources_per_skill]

    body_bytes = body.encode("utf-8")
    # Binary/declared-asset resources keep metadata only and are not retained
    # in the snapshot, so they must not consume max_catalog_bytes.
    total = skill_size
    for path in paths:
        if candidates[path].get("kind") == "asset":
            continue
        try:
            total += (root / path).stat().st_size
        except OSError:
            warnings.append(f"resource skipped (cannot stat): {path}")
    if total > budget_bytes:
        return None, tuple(warnings + [
            f"skill {name!r} exceeds catalog budget ({total} > {budget_bytes})"
        ])

    resources: list[SkillResource] = []
    for path in paths:
        meta = candidates[path]
        target = root / path
        try:
            resource_size = target.stat().st_size
        except OSError:
            warnings.append(f"resource skipped (cannot stat): {path}")
            continue
        if resource_size > limits.max_resource_bytes:
            resources.append(SkillResource(
                path=path,
                kind=meta.get("kind") or "asset",
                description=meta.get("description", ""),
                size=resource_size,
                sha256=None,
                readable=False,
                content=None,
            ))
            continue
        try:
            raw = target.read_bytes()
        except OSError:
            warnings.append(f"resource skipped (cannot read): {path}")
            continue
        digest = hashlib.sha256(raw).hexdigest()
        binary = b"\x00" in raw
        content: Optional[str] = None
        if not binary:
            try:
                content = raw.decode("utf-8")
            except UnicodeDecodeError:
                binary = True
        kind = meta.get("kind") or ("asset" if binary else "reference")
        if binary or kind == "asset":
            kind = "asset"
            readable = False
            content = None
        else:
            readable = True
        resources.append(SkillResource(
            path=path,
            kind=kind,
            description=meta.get("description", ""),
            size=resource_size,
            sha256=digest,
            readable=readable,
            content=content,
        ))

    retained = len(body_bytes) + sum(
        len(resource.content.encode("utf-8"))
        for resource in resources
        if resource.readable and resource.content is not None
    )
    if retained > budget_bytes:
        return None, tuple(warnings + [
            f"skill {name!r} exceeds catalog budget after loading "
            f"({retained} > {budget_bytes})"
        ])

    entry = SkillEntry(
        name=name,
        description=description,
        when_to_use=when_to_use,
        version=version,
        source_id=package.source_id,
        priority=package.priority,
        body=body,
        body_sha256=hashlib.sha256(body_bytes).hexdigest(),
        body_size=len(body_bytes),
        resources=tuple(resources),
    )
    return entry, tuple(warnings)


__all__ = [
    "RESOURCE_DIRS",
    "SKILL_NAME_PATTERN",
    "VERSION_PATTERN",
    "load_package",
    "normalise_display_text",
    "sanitise_version",
]
