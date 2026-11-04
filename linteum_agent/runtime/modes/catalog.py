"""Build the immutable mode catalog (§6.2/§6.3).

Discovery reads plugin-materialised files (builtin definitions come from
code), validates each document, resolves ``extends`` and applies limits.
Only the default mode's failure is fatal; every other invalid package is
recorded in ``ModeCatalog.invalid`` and surfaced by ``mode.list``.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from typing import Mapping, Optional, Sequence

from .builtin import BUILTIN_MODES, BUILTIN_NAMES
from .errors import ModeError, ModeExtends, ModeInvalid, ModeReserved
from .model import (
    ModeCatalog,
    ModeDefinition,
    ModeEntry,
    ModeIssue,
    ModeLimits,
    ModePackage,
    ModeSource,
    SkillSelection,
    ToolSelection,
)
from .parsing import read_mode_package

_MAX_EXTENDS_DEPTH = 8


def _issue(package: ModePackage, error: ModeError) -> ModeIssue:
    return ModeIssue(
        name=package.name,
        source_id=package.source_id,
        path=package.path,
        error=str(error),
        code=error.code,
    )


def _load_builtin(package: ModePackage) -> ModeDefinition:
    definition = BUILTIN_MODES.get(package.name)
    if definition is None:
        raise ModeInvalid(f"unknown builtin mode: {package.name!r}")
    return definition


def _load_plugin(package: ModePackage, limits: ModeLimits) -> ModeDefinition:
    if package.name in BUILTIN_NAMES:
        raise ModeReserved(
            f"mode name {package.name!r} is reserved for a builtin mode",
            key="name",
        )
    if package.path is None:
        raise ModeInvalid(f"mode {package.name!r} has no materialised file")
    if package.path.name.endswith(".md") and package.path.stem != package.name:
        raise ModeInvalid(
            "mode name must equal the file stem "
            f"({package.path.stem!r} != {package.name!r})",
            key="name",
        )
    return read_mode_package(package, limits)


def _merge(parent: ModeDefinition, child: ModeDefinition) -> ModeDefinition:
    """Merge one child definition over its parent (§5.4)."""
    if child.prompt_mode == "replace":
        prompt = child.system_prompt
    elif child.system_prompt:
        prompt = f"{parent.system_prompt}\n\n{child.system_prompt}"
    else:
        prompt = parent.system_prompt

    description = child.description or parent.description

    if child.tools_provided:
        include = (
            child.tools.include
            if child.tools.include is not None
            else parent.tools.include
        )
        exclude = (
            parent.tools.exclude if child.tools.include is None else ()
        ) + child.tools.exclude
        tools = ToolSelection(include=include, exclude=exclude)
    else:
        tools = parent.tools

    if child.skills_provided:
        include = (
            child.skills.include
            if child.skills.include is not None
            else parent.skills.include
        )
        exclude = (
            parent.skills.exclude if child.skills.include is None else ()
        ) + child.skills.exclude
        skills = SkillSelection(
            enabled=child.skills.enabled,
            include=include,
            exclude=exclude,
        )
    else:
        skills = parent.skills

    return replace(
        child,
        description=description,
        system_prompt=prompt,
        tools=tools,
        skills=skills,
        tools_provided=True,
        skills_provided=True,
    )


def _resolve_definitions(
    winners: Mapping[str, ModeEntry],
) -> tuple[dict[str, ModeEntry], list[ModeIssue]]:
    """Resolve every ``extends`` chain; invalid entries are removed."""
    resolved: dict[str, ModeDefinition] = {}
    failed: dict[str, ModeError] = {}

    def validate_chain(name: str) -> None:
        """Reject cycles/depth/missing parents before the cached resolver."""
        chain: list[str] = []
        seen: set[str] = set()
        current: Optional[str] = name
        while current is not None:
            if current in seen:
                text = " -> ".join((*chain, current))
                raise ModeExtends(
                    f"mode extends cycle: {text}", key="extends",
                    context={"chain": text},
                )
            if len(chain) >= _MAX_EXTENDS_DEPTH:
                text = " -> ".join((*chain, current))
                raise ModeExtends(
                    f"mode extends chain exceeds {_MAX_EXTENDS_DEPTH}",
                    key="extends", context={"chain": text},
                )
            entry = winners.get(current)
            if entry is None:
                raise ModeExtends(
                    f"parent mode {current!r} does not exist",
                    key="extends",
                )
            seen.add(current)
            chain.append(current)
            current = entry.definition.extends

    for name in sorted(winners):
        try:
            validate_chain(name)
        except ModeError as exc:
            failed[name] = exc

    def resolve(name: str, stack: tuple[str, ...]) -> ModeDefinition:
        if name in resolved:
            return resolved[name]
        if name in failed:
            raise failed[name]
        if name in stack:
            chain = " -> ".join((*stack, name))
            error = ModeExtends(
                f"mode extends cycle: {chain}", key="extends",
                context={"chain": chain},
            )
            failed[name] = error
            raise error
        if len(stack) >= _MAX_EXTENDS_DEPTH:
            error = ModeExtends(
                f"mode extends chain exceeds {_MAX_EXTENDS_DEPTH}",
                key="extends",
                context={"chain": " -> ".join((*stack, name))},
            )
            failed[name] = error
            raise error
        entry = winners.get(name)
        if entry is None:
            error = ModeExtends(
                f"mode {name!r} does not exist",
                key="extends",
            )
            failed[name] = error
            raise error
        definition = entry.definition
        if definition.extends:
            try:
                parent = resolve(definition.extends, (*stack, name))
            except ModeError as exc:
                failed[name] = exc
                raise
            definition = _merge(parent, definition)
        resolved[name] = definition
        return definition

    issues: list[ModeIssue] = []
    for name in sorted(winners):
        try:
            resolve(name, ())
        except ModeError as exc:
            entry = winners[name]
            issues.append(ModeIssue(
                name=name,
                source_id=entry.source_id,
                path=entry.path,
                error=str(exc),
                code=exc.code,
            ))

    entries: dict[str, ModeEntry] = {}
    for name, entry in winners.items():
        if name in failed:
            continue
        definition = resolved.get(name, entry.definition)
        entries[name] = replace(entry, definition=definition)
    return entries, issues


def _body_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _catalog_digest(entries: Mapping[str, ModeEntry]) -> str:
    payload = {
        "modes": [
            {
                "name": name,
                "source": entry.source_id,
                "priority": entry.priority,
                "description": entry.definition.description,
                "extends": entry.definition.extends,
                "prompt_mode": entry.definition.prompt_mode,
                "prompt_sha256": _body_sha256(entry.definition.system_prompt),
                "tools_include": list(entry.definition.tools.include)
                if entry.definition.tools.include is not None else None,
                "tools_exclude": list(entry.definition.tools.exclude),
                "skills_enabled": entry.definition.skills.enabled,
                "skills_include": list(entry.definition.skills.include)
                if entry.definition.skills.include is not None else None,
                "skills_exclude": list(entry.definition.skills.exclude),
            }
            for name, entry in sorted(entries.items())
        ]
    }
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()


async def load_mode_catalog(
    sources: Sequence[ModeSource],
    *,
    limits: ModeLimits,
) -> ModeCatalog:
    """Discover, validate, merge and hash every available mode."""
    warnings: list[str] = []
    invalid: list[ModeIssue] = []
    candidates: list[tuple[int, ModePackage, Optional[ModeDefinition]]] = []

    for order, source in enumerate(sources):
        try:
            discovery = await source.discover()
        except Exception as exc:  # noqa: BLE001 - one source must not block
            warnings.append(
                f"mode source {getattr(source, 'source_id', '?')} failed: "
                f"{type(exc).__name__}: {exc}"
            )
            continue
        warnings.extend(discovery.warnings)
        for package in discovery.packages:
            try:
                definition = (
                    _load_builtin(package) if package.builtin
                    else _load_plugin(package, limits)
                )
            except ModeError as exc:
                invalid.append(_issue(package, exc))
                warnings.append(
                    f"mode {package.name!r} from {package.source_id} is "
                    f"invalid ({exc.code}): {exc}"
                )
                continue
            candidates.append((order, package, definition))

    # Winner per name: highest priority first, then source order.
    winners: dict[str, ModeEntry] = {}
    winner_rank: dict[str, tuple[int, int]] = {}
    for order, package, definition in candidates:
        rank = (-package.priority, order)
        old_rank = winner_rank.get(package.name)
        if old_rank is None:
            winner_rank[package.name] = rank
            winners[package.name] = ModeEntry(
                definition=definition,
                source_id=package.source_id,
                priority=package.priority,
                path=package.path,
            )
            continue
        if rank < old_rank:
            loser = winners[package.name]
            warnings.append(
                f"mode {package.name!r} from {package.source_id} overrides "
                f"{loser.source_id}"
            )
            winner_rank[package.name] = rank
            winners[package.name] = ModeEntry(
                definition=definition,
                source_id=package.source_id,
                priority=package.priority,
                path=package.path,
            )
        else:
            warnings.append(
                f"mode {package.name!r} from {package.source_id} is "
                f"duplicate; {winners[package.name].source_id} wins"
            )

    entries, extends_issues = _resolve_definitions(winners)
    invalid.extend(extends_issues)
    for issue in extends_issues:
        warnings.append(
            f"mode {issue.name!r} from {issue.source_id} is invalid "
            f"({issue.code}): {issue.error}"
        )

    # max_modes applies to custom (plugin) entries only.
    custom = sorted(
        (name for name, entry in entries.items() if entry.source_id != "builtin"),
        key=lambda name: (-entries[name].priority, name),
    )
    if len(custom) > limits.max_modes:
        for name in custom[limits.max_modes:]:
            entries.pop(name, None)
        warnings.append(
            f"{len(custom) - limits.max_modes} mode(s) omitted "
            f"(max_modes={limits.max_modes})"
        )

    ordered = {name: entries[name] for name in sorted(entries)}
    return ModeCatalog(
        entries=ordered,
        invalid=tuple(invalid),
        warnings=tuple(warnings),
        digest=_catalog_digest(ordered),
    )


__all__ = ["load_mode_catalog"]
