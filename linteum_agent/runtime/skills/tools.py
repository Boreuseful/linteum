"""The two leaf tools that disclose a skill: ``load_skill`` / ``read_skill_resource``.

Both tools close over an immutable :class:`SkillCatalog` and only read the
in-memory snapshot; they never open files, publish events or inspect
``AgentState``.  Their output is deliberately bounded by
``catalog.limits.max_result_bytes`` (default 7 KiB), which stays below
``ToolRuntime``'s 8 KiB externalisation threshold — otherwise the provider
would only see an ``art:`` placeholder instead of the skill content.

Chunking is byte-oriented but UTF-8-safe: an offset may land in the middle of a
multi-byte codepoint, so it is snapped back to a character boundary before
decoding.  Every chunk carries the actual byte range and, when more remains, a
``[next]`` call the model can repeat.
"""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Callable, Optional

from ..tools.decorator import tool
from ..tools.spec import ToolSpec
from .errors import (
    SkillLimitExceeded,
    SkillNotFound,
    SkillPathViolation,
    SkillResourceNotFound,
)
from .model import SkillCatalog, SkillEntry, SkillResource

#: Smallest content slice worth returning; below this we refuse the chunk
#: (a header-only result would look like a real page with no instructions).
_MIN_CONTENT_BYTES = 128


def _byte_len(text: str) -> int:
    return len(text.encode("utf-8"))


def _truncate_utf8(text: str, budget: int) -> str:
    data = text.encode("utf-8")
    if len(data) <= budget:
        return text
    return data[: max(0, budget)].decode("utf-8", "ignore")


def _slice_utf8(text: str, start: int, max_bytes: int) -> tuple[str, int, int]:
    """Return ``(chunk, next_offset, total_bytes)`` on codepoint boundaries."""
    data = text.encode("utf-8")
    total = len(data)
    if start >= total:
        return "", total, total
    start = max(0, min(start, total))
    while start > 0 and (data[start] & 0xC0) == 0x80:
        start -= 1
    end = min(total, start + max(1, max_bytes))
    while end < total and (data[end] & 0xC0) == 0x80:
        end -= 1
    if end <= start:  # a single codepoint is wider than the requested slice
        end = start + 1
        while end < total and (data[end] & 0xC0) == 0x80:
            end += 1
    return data[start:end].decode("utf-8"), end, total


def _format_size(size: int) -> str:
    if size >= 1024:
        return f"{size / 1024:.1f} KiB"
    return f"{size} B"


def _resource_line(resource: SkillResource, description_cap: Optional[int] = None) -> str:
    description = resource.description
    if description_cap is not None and len(description) > description_cap:
        description = description[:description_cap].rstrip()
        if description:
            description += "…"
    suffix = f" — {description}" if description else ""
    return (
        f"- {resource.path} ({resource.kind}, {_format_size(resource.size)})"
        f"{suffix}\n"
    )


def _resources_block(entry: SkillEntry, budget: int) -> str:
    """List **every** admitted resource, trimming descriptions to fit.

    The first ``load_skill`` chunk is the only discovery surface for resource
    paths, so hiding resources behind a fixed line cap would make them
    unreadable by construction.  Paths are therefore preserved; descriptions
    are shortened first, then the list falls back to a compact form.
    """
    if not entry.resources or budget <= 0:
        return ""
    header = "[resources]\n"
    for cap in (None, 256, 128, 64, 32, 0):
        text = header + "".join(
            _resource_line(resource, cap) for resource in entry.resources
        ) + "\n"
        if _byte_len(text) <= budget:
            return text
    text = header + "".join(f"- {resource.path}\n" for resource in entry.resources) + "\n"
    if _byte_len(text) <= budget:
        return text
    return _truncate_utf8(
        header + ", ".join(resource.path for resource in entry.resources) + "\n\n",
        budget,
    )


def _render_bounded(
    text: str,
    offset: int,
    budget: int,
    header: Callable[[int, int, int], str],
    footer: Callable[[int], str],
) -> str:
    """Render one chunk whose UTF-8 length is at most ``budget``."""
    total = _byte_len(text)
    if offset >= total:
        note = (
            f"offset {offset} is beyond the end of this content "
            f"(total {total} bytes).\n"
        )
        return _truncate_utf8(header(offset, offset, total) + note, budget)

    estimated_overhead = _byte_len(header(0, 0, total)) + _byte_len(footer(0)) + 4
    size = max(1, budget - estimated_overhead)
    while True:
        chunk, next_offset, total = _slice_utf8(text, offset, size)
        output = header(offset, next_offset, total) + chunk
        if next_offset < total:
            output += footer(next_offset)
        used = _byte_len(output)
        if used <= budget:
            return output
        if size <= 1:
            note = "the configured max_result_bytes is too small for this content.\n"
            return _truncate_utf8(header(offset, offset, total) + note, budget)
        size = max(1, size - max(1, used - budget))


def _normalise_resource_path(value: str) -> str:
    """Validate the model-supplied path; traversal is a safety violation."""
    if not isinstance(value, str) or not value.strip():
        raise SkillPathViolation("resource path must be a non-empty string")
    if "\x00" in value or "\\" in value:
        raise SkillPathViolation("invalid resource path", kind="invalid_path")
    if value.startswith(("/", "~")):
        raise SkillPathViolation("absolute paths are not allowed", kind="absolute_path")
    path = PurePosixPath(value)
    if path.is_absolute():
        raise SkillPathViolation("absolute paths are not allowed", kind="absolute_path")
    if value.strip() in (".", "..") or any(
        part in ("", ".", "..") or part.startswith(".")
        for part in path.parts
    ):
        raise SkillPathViolation("path traversal is not allowed", kind="escape")
    return path.as_posix()


def _resource_text(
    entry: SkillEntry,
    resource: SkillResource,
    offset: int,
    budget: int,
) -> str:
    prefix = (
        "[script read-only; execution is not enabled in v1]\n"
        if resource.kind == "script" else ""
    )

    def header(start: int, end: int, total: int) -> str:
        return (
            f"[skill {entry.name} | resource {resource.path} | {resource.kind} "
            f"| bytes {start}-{end}/{total}]\n\n{prefix}"
        )

    def footer(next_offset: int) -> str:
        return (
            f'\n[next] read_skill_resource(name="{entry.name}", '
            f'path="{resource.path}", offset={next_offset})\n'
        )

    if not resource.content:
        return _truncate_utf8(header(0, 0, 0) + "(empty resource)\n", budget)
    return _render_bounded(resource.content, offset, budget, header, footer)


def make_skill_tools(catalog: SkillCatalog) -> list[ToolSpec]:
    """Build the two loader tools bound to ``catalog`` (empty catalog -> none)."""
    if not catalog:
        return []

    budget = catalog.limits.max_result_bytes

    @tool(
        "Load a skill's instructions by name, in chunks. Call only when the "
        "current task matches a skill description.",
        {
            "name": {"type": "string", "description": "skill name from the L0 index"},
            "offset": {
                "type": "integer", "minimum": 0,
                "description": "byte offset returned by the previous chunk (default 0)",
            },
        },
        required=["name"], side_effect="read", idempotent=True,
        timeout=10, concurrency=4, name="load_skill",
    )
    def load_skill(name: str, offset: int = 0) -> str:
        entry = catalog.get(name)
        if entry is None:
            raise SkillNotFound(name)
        if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
            raise SkillLimitExceeded("offset must be an integer >= 0")

        resources = ""
        if offset == 0:
            header_base = (
                f"[skill {entry.name} | source={entry.source_id} "
                f"| version={entry.version} | bytes 0-0/0]\n\n"
            )
            footer_reserve = _byte_len(
                f'\n[next] load_skill(name="{entry.name}", offset=999999999)\n'
            )
            resource_budget = (
                budget - _byte_len(header_base) - footer_reserve
                - _MIN_CONTENT_BYTES - 16
            )
            if resource_budget < 0:
                raise SkillLimitExceeded(
                    "max_result_bytes is too small for a skill chunk"
                )
            resources = _resources_block(entry, resource_budget)
            if entry.resources and not resources:
                raise SkillLimitExceeded(
                    "max_result_bytes is too small to list skill resources"
                )

        def header(start: int, end: int, total: int) -> str:
            return (
                f"[skill {entry.name} | source={entry.source_id} "
                f"| version={entry.version} | bytes {start}-{end}/{total}]\n\n"
                f"{resources}"
            )

        def footer(next_offset: int) -> str:
            return (
                f'\n[next] load_skill(name="{entry.name}", '
                f"offset={next_offset})\n"
            )

        if _byte_len(header(0, 0, 0)) + _byte_len(footer(0)) + _MIN_CONTENT_BYTES > budget:
            raise SkillLimitExceeded(
                "max_result_bytes is too small for a skill chunk"
            )
        if not entry.body:
            return _truncate_utf8(header(0, 0, 0) + "(empty skill body)\n", budget)
        return _render_bounded(entry.body, offset, budget, header, footer)

    @tool(
        "Read one text resource from a skill, in chunks. Only paths listed by "
        "`load_skill` are allowed.",
        {
            "name": {"type": "string", "description": "skill name"},
            "path": {
                "type": "string",
                "description": "relative resource path from the skill's resource list",
            },
            "offset": {
                "type": "integer", "minimum": 0,
                "description": "byte offset returned by the previous chunk (default 0)",
            },
        },
        required=["name", "path"], side_effect="read", idempotent=True,
        timeout=10, concurrency=4, name="read_skill_resource",
    )
    def read_skill_resource(name: str, path: str, offset: int = 0) -> str:
        entry = catalog.get(name)
        if entry is None:
            raise SkillNotFound(name)
        if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
            raise SkillLimitExceeded("offset must be an integer >= 0")
        normalised = _normalise_resource_path(path)
        resource = next(
            (item for item in entry.resources if item.path == normalised), None
        )
        if resource is None:
            raise SkillResourceNotFound(normalised)
        if not resource.readable:
            if resource.kind == "asset":
                digest = resource.sha256 or "unavailable"
                text = (
                    f"[skill {entry.name} | resource {resource.path} | asset | "
                    f"{_format_size(resource.size)} | sha256:{digest}]\n"
                    "Binary resources are not readable in v1. This asset is "
                    "listed for discovery only.\n"
                )
                return _truncate_utf8(text, budget)
            raise SkillLimitExceeded(
                f"resource is not readable (exceeds max_resource_bytes): "
                f"{resource.path}"
            )
        return _resource_text(entry, resource, offset, budget)

    return [load_skill.__tool_spec__, read_skill_resource.__tool_spec__]


__all__ = ["make_skill_tools"]
