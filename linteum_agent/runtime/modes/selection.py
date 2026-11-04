"""Tool/skill selector resolution and the pure ``resolve_mode`` step (§5.3/§7).

The resolver never performs I/O and never creates a ``ToolRegistry``.  It
takes the host's available specs and the session's full skill catalog, applies
the mode's selectors, renders the prompt/environment blocks and returns a
pure :class:`ModeResolution`.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from typing import Optional, Sequence

from ..skills.catalog import build_catalog, filter_catalog
from ..skills.model import SkillCatalog
from ..skills.tools import make_skill_tools
from ..tools.spec import ToolSpec
from ..tools.workspace import Workspace
from .model import (
    ModeDefinition,
    ModeLimits,
    ModeResolution,
    ModeSnapshot,
    ToolSelection,
)
from .parsing import validate_tool_selector
from .prompt import (
    render_environment_block,
    render_prompt,
    template_variables,
)

FILE_TOOLS = frozenset({"read_file", "write_file", "edit_file", "list_dir"})
SHELL_TOOLS = frozenset({"bash"})
SKILL_TOOLS = frozenset({"load_skill", "read_skill_resource"})

_TOOL_GROUPS = frozenset(
    {"all", "builtin", "builtin.file", "builtin.shell", "skills",
     "subagent", "mcp", "plugin"}
)


def classify_tool(
    spec: ToolSpec, *, subagent_tool_name: Optional[str] = None
) -> Optional[str]:
    """Return the selector group for one host tool (§7.3)."""
    if spec.name in FILE_TOOLS:
        return "builtin.file"
    if spec.name in SHELL_TOOLS:
        return "builtin.shell"
    if spec.name in SKILL_TOOLS:
        return "skills"
    if subagent_tool_name is not None and spec.name == subagent_tool_name:
        return "subagent"
    if spec.name.startswith("mcp__"):
        return "mcp"
    if spec.plugin_id:
        return "plugin"
    return None


def _is_exact_selector(selector: str) -> bool:
    return (
        selector not in _TOOL_GROUPS
        and not selector.startswith(("mcp:", "plugin:"))
    )


def _selector_matches(
    spec: ToolSpec, selector: str, subagent_tool_name: Optional[str],
) -> bool:
    group = classify_tool(spec, subagent_tool_name=subagent_tool_name)
    if selector == "all":
        return True
    if selector == "builtin":
        return group in ("builtin.file", "builtin.shell")
    if selector in ("builtin.file", "builtin.shell", "skills", "mcp", "plugin"):
        return group == selector
    if selector == "subagent":
        return (
            subagent_tool_name is not None
            and spec.name == subagent_tool_name
        )
    if selector.startswith("mcp:"):
        server = selector[len("mcp:"):]
        return spec.name.startswith(f"mcp__{server}__")
    if selector.startswith("plugin:"):
        return spec.plugin_id == selector[len("plugin:"):]
    return spec.name == selector


def parse_tool_selectors(
    selection: ToolSelection,
) -> tuple[Optional[tuple[str, ...]], tuple[str, ...]]:
    """Validate and dedupe include/exclude selectors (first occurrence wins)."""
    def clean(values: Optional[Sequence[str]]) -> Optional[tuple[str, ...]]:
        if values is None:
            return None
        result: list[str] = []
        seen: set[str] = set()
        for value in values:
            text = validate_tool_selector(value)
            if text not in seen:
                seen.add(text)
                result.append(text)
        return tuple(result)

    return clean(selection.include), clean(selection.exclude) or ()


def _group_included(selection: ToolSelection, group: str) -> bool:
    include = selection.include
    exclude = selection.exclude
    if "all" in exclude or group in exclude:
        return False
    if include is None:
        return True
    return "all" in include or group in include


def _skills_group_requested(selection: ToolSelection) -> bool:
    if not _group_included(selection, "skills"):
        # An exact loader name is not enough: the pair is atomic.
        return False
    # Excluding either half breaks the atomic pair just like excluding the
    # group; the index must not advertise tools the model cannot call.
    if any(name in selection.exclude for name in SKILL_TOOLS):
        return False
    include = selection.include
    if include is None or "all" in include or "skills" in include:
        return True
    return all(name in include for name in SKILL_TOOLS)


def _resolve_tools(
    selection: ToolSelection,
    specs: Sequence[ToolSpec],
    *,
    subagent_tool_name: Optional[str],
) -> tuple[list[ToolSpec], list[str]]:
    warnings: list[str] = []
    include, exclude = parse_tool_selectors(selection)

    if include is None:
        selected_names = {spec.name for spec in specs}
    else:
        selected_names: set[str] = set()
        for selector in include:
            matched = [
                spec.name for spec in specs
                if _selector_matches(spec, selector, subagent_tool_name)
            ]
            if not matched and _is_exact_selector(selector):
                warnings.append(
                    f"tool selector {selector!r} matched no available tool"
                )
            selected_names.update(matched)

    excluded_names: set[str] = set()
    for selector in exclude:
        matched = [
            spec.name for spec in specs
            if _selector_matches(spec, selector, subagent_tool_name)
        ]
        if not matched and _is_exact_selector(selector):
            warnings.append(
                f"tool selector {selector!r} matched no available tool"
            )
        excluded_names.update(matched)

    selected = [
        spec for spec in specs
        if spec.name in selected_names and spec.name not in excluded_names
    ]
    return selected, warnings


def _empty_skills(limits) -> SkillCatalog:
    return build_catalog((), limits=limits)


def _snapshot_digest(snapshot: ModeSnapshot) -> str:
    blob = json.dumps(
        snapshot.to_dict(), ensure_ascii=False, sort_keys=True,
        separators=(",", ":"),
    )
    return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()


def resolve_mode(
    definition: ModeDefinition,
    *,
    workspace: Workspace,
    full_skills: SkillCatalog,
    base_tools: Sequence[ToolSpec],
    subagent_enabled: bool,
    subagent_tool_name: str,
    limits: ModeLimits,
) -> ModeResolution:
    """Resolve one effective mode definition against host capabilities."""
    warnings: list[str] = []

    if definition.skills.enabled:
        filtered_skills, skill_warnings = filter_catalog(
            full_skills,
            include=definition.skills.include,
            exclude=definition.skills.exclude,
        )
        warnings.extend(skill_warnings)
    else:
        filtered_skills = _empty_skills(full_skills.limits)

    if filtered_skills.entries and not _skills_group_requested(definition.tools):
        warnings.append(
            "skills are visible but the skills loader group is not selected; "
            "the index is suppressed"
        )
        filtered_skills = replace(filtered_skills, index_text="")
        skill_specs: list[ToolSpec] = []
    elif filtered_skills.entries:
        skill_specs = list(make_skill_tools(filtered_skills))
    else:
        skill_specs = []

    include = definition.tools.include
    if include is not None:
        named = [name for name in SKILL_TOOLS if name in include]
        if named and not _skills_group_requested(definition.tools):
            warnings.append(
                "load_skill and read_skill_resource must be selected together "
                "as the 'skills' group"
            )

    available = [*base_tools, *skill_specs]
    resolved_specs, tool_warnings = _resolve_tools(
        definition.tools,
        available,
        subagent_tool_name=subagent_tool_name,
    )
    warnings.extend(tool_warnings)

    subagent_on = bool(
        subagent_enabled and _group_included(definition.tools, "subagent")
    )
    subagent_tools = (
        frozenset({subagent_tool_name}) if subagent_on else frozenset()
    )

    tool_names = tuple(spec.name for spec in resolved_specs)
    skill_names = tuple(entry.name for entry in filtered_skills.entries)
    shell_available = any(spec.name in SHELL_TOOLS for spec in resolved_specs)

    variables = template_variables(
        mode_name=definition.name,
        description=definition.description,
        workspace=workspace,
        shell_available=shell_available,
    )
    system_prompt = render_prompt(definition.system_prompt, variables)
    environment_block = render_environment_block(
        name=definition.name,
        description=definition.description,
        workspace=workspace,
        shell_available=shell_available,
    )

    snapshot = ModeSnapshot(
        schema=1,
        name=definition.name,
        description=definition.description,
        source_id=definition.source_id,
        extends=definition.extends,
        prompt_mode=definition.prompt_mode,
        system_prompt=system_prompt,
        environment_block=environment_block,
        tools_include=definition.tools.include,
        tools_exclude=definition.tools.exclude,
        skills_enabled=definition.skills.enabled,
        skills_include=definition.skills.include,
        skills_exclude=definition.skills.exclude,
        resolved_tools=tool_names,
        subagent=subagent_on,
    )
    digest = _snapshot_digest(snapshot)

    return ModeResolution(
        name=definition.name,
        description=definition.description,
        source_id=definition.source_id,
        digest=digest,
        snapshot=snapshot,
        system_prompt=system_prompt,
        environment_block=environment_block,
        tool_specs=tuple(resolved_specs),
        subagent_tools=subagent_tools,
        filtered_skills=filtered_skills,
        tool_names=tool_names,
        skill_names=skill_names,
        warnings=tuple(warnings),
    )


__all__ = [
    "FILE_TOOLS",
    "SHELL_TOOLS",
    "SKILL_TOOLS",
    "classify_tool",
    "parse_tool_selectors",
    "resolve_mode",
]
