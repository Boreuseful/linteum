"""Parse one mode document into a :class:`ModeDefinition` (§5.2/§5.5).

Only plugin-delivered files reach this module.  Parsing is fail-loud at the
catalog boundary: an invalid document raises :class:`ModeError`, and the
catalog records it as an ``invalid`` entry without blocking other modes.
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path
from typing import Any, Mapping, Optional

import yaml

from .errors import ModeInvalid, ModeLimit
from .model import (
    ModeDefinition,
    ModeLimits,
    ModePackage,
    SkillSelection,
    ToolSelection,
)

MODE_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_TOOL_EXACT_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SOURCE_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._:-]{0,127}$")
_SKILL_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_VAR_PATTERN = re.compile(r"\{\{\s*([a-z_][a-z0-9_]*)\s*\}\}")

_KNOWN_FIELDS = frozenset(
    {"version", "description", "extends", "prompt_mode", "tools", "skills"}
)
_TOOL_GROUPS = frozenset({"all", "builtin", "skills", "subagent", "mcp", "plugin"})
_TOOL_DOTTED = frozenset({"builtin.file", "builtin.shell"})
_MAX_DESCRIPTION = 512


def _one_line(value: Any, *, limit: int = _MAX_DESCRIPTION) -> str:
    """Collapse frontmatter text into one safe display line."""
    if not isinstance(value, str):
        value = str(value)
    text = value.replace("\r", " ").replace("\n", " ").replace("\t", " ")
    text = "".join(
        ch for ch in text if unicodedata.category(ch) not in ("Cc", "Cf")
    )
    return " ".join(text.split())[:limit]


def _invalid(message: str, *, field: Optional[str] = None) -> ModeInvalid:
    return ModeInvalid(message, key=field)


def _optional_strings(value: Any, *, field: str) -> Optional[tuple[str, ...]]:
    if value is None:
        return None
    if not isinstance(value, (list, tuple)):
        raise _invalid(f"{field} must be a list of strings", field=field)
    result: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            raise _invalid(f"{field}[{index}] must be a non-empty string",
                           field=field)
        result.append(item.strip())
    return tuple(result)


def validate_tool_selector(selector: Any) -> str:
    """Validate one tool-selector string and return it stripped."""
    if not isinstance(selector, str) or not selector.strip():
        raise _invalid("tool selector must be a non-empty string",
                       field="tools")
    text = selector.strip()
    if ":" in text:
        prefix, _, value = text.partition(":")
        if prefix not in ("mcp", "plugin") or not value:
            raise _invalid(f"invalid tool selector: {text!r}", field="tools")
        if not _SOURCE_ID_PATTERN.match(value):
            raise _invalid(f"invalid tool selector: {text!r}", field="tools")
        return text
    if text in _TOOL_GROUPS or text in _TOOL_DOTTED:
        return text
    if "." in text and text not in _TOOL_DOTTED:
        # Dotted names are allowed as exact tool names (MCP/plugin tools can
        # contain dots); only builtin.* is a group.
        pass
    if not _TOOL_EXACT_PATTERN.match(text):
        raise _invalid(f"invalid tool selector: {text!r}", field="tools")
    return text


def validate_skill_selector(selector: Any) -> str:
    """Validate one skills-selector string and return it stripped."""
    if not isinstance(selector, str) or not selector.strip():
        raise _invalid("skill selector must be a non-empty string",
                       field="skills")
    text = selector.strip()
    if text == "all" or _SKILL_NAME_PATTERN.match(text):
        return text
    if text.startswith("source:"):
        source = text[len("source:"):]
        if source and _SOURCE_ID_PATTERN.match(source):
            return text
    raise _invalid(f"invalid skill selector: {text!r}", field="skills")


def _parse_tools(raw: Any) -> ToolSelection:
    if raw is None:
        return ToolSelection()
    if not isinstance(raw, Mapping):
        raise _invalid("tools must be a mapping", field="tools")
    unknown = sorted(set(raw) - {"include", "exclude"})
    if unknown:
        raise _invalid(f"unknown tools field(s): {', '.join(unknown)}",
                       field="tools")
    include = _optional_strings(raw.get("include"), field="tools.include")
    exclude = _optional_strings(raw.get("exclude"), field="tools.exclude") or ()
    if include is not None:
        include = tuple(validate_tool_selector(item) for item in include)
    exclude = tuple(validate_tool_selector(item) for item in exclude)
    return ToolSelection(include=include, exclude=exclude)


def _parse_skills(raw: Any) -> SkillSelection:
    if raw is None:
        return SkillSelection()
    if not isinstance(raw, Mapping):
        raise _invalid("skills must be a mapping", field="skills")
    unknown = sorted(set(raw) - {"enabled", "include", "exclude"})
    if unknown:
        raise _invalid(f"unknown skills field(s): {', '.join(unknown)}",
                       field="skills")
    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        raise _invalid("skills.enabled must be a boolean", field="skills.enabled")
    include = _optional_strings(raw.get("include"), field="skills.include")
    exclude = _optional_strings(raw.get("exclude"), field="skills.exclude") or ()
    if include is not None:
        include = tuple(validate_skill_selector(item) for item in include)
    exclude = tuple(validate_skill_selector(item) for item in exclude)
    return SkillSelection(enabled=enabled, include=include, exclude=exclude)


def _split_document(text: str) -> tuple[dict, str]:
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        raise _invalid("mode document must start with '---'")
    end = next(
        (index for index in range(1, len(lines)) if lines[index].strip() == "---"),
        None,
    )
    if end is None:
        raise _invalid("mode frontmatter is not terminated")
    try:
        data = yaml.safe_load("".join(lines[1:end]))
    except Exception as exc:  # noqa: BLE001 - malformed YAML is user input
        raise _invalid(f"invalid YAML frontmatter: {exc}") from exc
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise _invalid("mode frontmatter must be a YAML mapping")
    return data, "".join(lines[end + 1:])


def parse_mode_document(
    text: str,
    *,
    name: str,
    source_id: str,
    priority: int,
    path: Optional[Path],
    limits: ModeLimits,
) -> ModeDefinition:
    """Parse a UTF-8 mode document (frontmatter + body)."""
    if not isinstance(name, str) or not MODE_NAME_PATTERN.match(name):
        raise _invalid(f"invalid mode name: {name!r}", field="name")
    if not isinstance(text, str):
        raise _invalid("mode document must be text")
    if text.startswith("﻿"):
        text = text[1:]
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if "\x00" in text:
        raise _invalid("mode document contains NUL")
    if len(text.encode("utf-8")) > limits.max_mode_bytes:
        raise ModeLimit(
            f"mode file exceeds {limits.max_mode_bytes} bytes",
            key="max_mode_bytes",
            context={"name": name},
        )

    data, body = _split_document(text)
    unknown = sorted(
        key for key in data
        if key not in _KNOWN_FIELDS and not str(key).startswith("x-")
    )
    if unknown:
        raise _invalid(f"unknown mode field(s): {', '.join(unknown)}")
    if "name" in data:
        raise _invalid("mode name comes from the file name, not frontmatter",
                       field="name")

    version = data.get("version", 1)
    if isinstance(version, bool) or not isinstance(version, int) or version != 1:
        raise _invalid("mode version must be 1", field="version")

    extends = data.get("extends")
    if extends is not None:
        if not isinstance(extends, str) or not MODE_NAME_PATTERN.match(extends):
            raise _invalid("extends must be a mode name", field="extends")

    prompt_mode = data.get("prompt_mode", "append")
    if prompt_mode not in ("append", "replace"):
        raise _invalid("prompt_mode must be 'append' or 'replace'",
                       field="prompt_mode")

    description = data.get("description", "")
    if not isinstance(description, str):
        raise _invalid("description must be a string", field="description")

    tools = _parse_tools(data.get("tools"))
    skills = _parse_skills(data.get("skills"))
    extra = {
        str(key): value for key, value in data.items()
        if str(key).startswith("x-")
    }
    return ModeDefinition(
        name=name,
        description=_one_line(description),
        system_prompt=body.strip(),
        extends=extends,
        prompt_mode=prompt_mode,
        tools=tools,
        skills=skills,
        source_id=source_id,
        priority=priority,
        path=path,
        extra=extra,
        tools_provided="tools" in data,
        skills_provided="skills" in data,
    )


def read_mode_package(
    package: ModePackage,
    limits: ModeLimits,
) -> ModeDefinition:
    """Read and parse one plugin-materialised mode file."""
    path = package.path
    if path is None:
        raise _invalid(
            f"mode {package.name!r} has no materialised file",
            field="path",
        )
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise _invalid(f"mode file is unreadable: {path}") from exc
    if size > limits.max_mode_bytes:
        raise ModeLimit(
            f"mode file exceeds {limits.max_mode_bytes} bytes",
            key="max_mode_bytes",
            context={"name": package.name, "path": str(path)},
        )
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise _invalid(f"mode file is unreadable: {path}") from exc
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise _invalid(f"mode file is not UTF-8: {path}") from exc
    return parse_mode_document(
        text,
        name=package.name,
        source_id=package.source_id,
        priority=package.priority,
        path=path,
        limits=limits,
    )


__all__ = [
    "MODE_NAME_PATTERN",
    "parse_mode_document",
    "read_mode_package",
    "validate_skill_selector",
    "validate_tool_selector",
]
