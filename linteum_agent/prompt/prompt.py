"""Loading and pre-filling of the system prompt.

The system prompt template lives in ``system_prompt_md/system_prompt.md`` and uses three
placeholders to mark the blocks that are **pre-filled at runtime**:

======================  =================================================================
Placeholder             Filled content
======================  =================================================================
``<tools_list>``        Currently available tools (``ToolRegistry`` / ``Tool`` list)
``<skills_list>``       Currently invocable skills (``SkillRegistry`` / ``SkillMetadata`` list)
``<system_env>``        User system environment (operating system and shell)
======================  =================================================================

``build_system_prompt()`` renders all three blocks into the template and returns text that can
be used directly as a system message; ``build_system_prompt_parts()`` additionally returns the
raw text of each block for debugging and testing.

Inputs are kept flexible (duck typing): ``tools`` accepts a ``ToolRegistry`` / a list of
``Tool`` / a list of dicts / a list of names; ``skills`` accepts a ``SkillRegistry`` / a list of
``SkillMetadata`` / a list of dicts. Unknown types are treated as iterable, and a missing value
(``None``) is treated as empty.

Usage::

    from linteum_agent.prompt import build_system_prompt

    prompt_text = build_system_prompt(
        tools=runner.registry,
        skills=runner.skill_registry,
    )
"""

from __future__ import annotations

import os
import platform
from pathlib import Path
from typing import Any, Mapping

__all__ = [
    "PROMPT_DIRNAME",
    "DEFAULT_TEMPLATE_FILENAME",
    "TOOLS_PLACEHOLDER",
    "SKILLS_PLACEHOLDER",
    "SYSTEM_ENV_PLACEHOLDER",
    "EMPTY_TOOLS_TEXT",
    "EMPTY_SKILLS_TEXT",
    "templates_dir",
    "template_path",
    "load_default_template",
    "load_template",
    "collect_system_env",
    "render_tools",
    "render_skills",
    "fill_placeholders",
    "build_system_prompt_parts",
    "build_system_prompt",
]

# Template directory name (next to this module) and the default template filename.
PROMPT_DIRNAME = "system_prompt_md"
DEFAULT_TEMPLATE_FILENAME = "system_prompt.md"

# Placeholder markers in the template; replacement matches the literal string.
TOOLS_PLACEHOLDER = "<tools_list>"
SKILLS_PLACEHOLDER = "<skills_list>"
SYSTEM_ENV_PLACEHOLDER = "<system_env>"

# Placeholder text written when a block is empty, so the template keeps no stray blank gap.
EMPTY_TOOLS_TEXT = "(no tools available)"
EMPTY_SKILLS_TEXT = "(no skills available)"


# ----------------------------------------------------------------------
# Template loading
# ----------------------------------------------------------------------


def templates_dir() -> Path:
    """Return the template directory (``<this module dir>/system_prompt_md``)."""
    return Path(__file__).resolve().parent / PROMPT_DIRNAME


def template_path(name: str = DEFAULT_TEMPLATE_FILENAME) -> Path:
    """Return the absolute path of the template file (default ``system_prompt_md/system_prompt.md``)."""
    return templates_dir() / name


def load_default_template(*, encoding: str = "utf-8") -> str:
    """Load the **default** system prompt template shipped with the package
    (``system_prompt_md/system_prompt.md``).

    Unlike ``load_template()``, this function takes no path and always reads the built-in
    default template, which is convenient when no custom template is needed. A missing file or
    a decode failure raises ``OSError`` / ``UnicodeDecodeError``.
    """
    return template_path().read_text(encoding=encoding)


def load_template(
    path: str | Path | None = None,
    *,
    encoding: str = "utf-8",
) -> str:
    """Load the system prompt template text.

    With ``path=None`` (the default) it loads the built-in default template (equivalent to
    ``load_default_template()``); when a path is given it reads that file. A missing file or a
    decode failure raises ``OSError`` / ``UnicodeDecodeError``, leaving the decision to the
    caller.
    """
    if path is None:
        return load_default_template(encoding=encoding)
    return Path(path).expanduser().read_text(encoding=encoding)


# ----------------------------------------------------------------------
# System environment (operating system + shell)
# ----------------------------------------------------------------------


def _current_shell() -> str:
    """Return the current shell (prefers ``$SHELL``, falls back to ``%COMSPEC%`` on Windows)."""
    shell = os.environ.get("SHELL")
    if shell:
        return shell
    if os.name == "nt":
        return os.environ.get("COMSPEC", "cmd.exe")
    return "sh"


def collect_system_env(
    *,
    extra: Mapping[str, Any] | None = None,
) -> str:
    """Collect and format the "user system environment" used to fill the ``<system_env>``
    placeholder.

    It contains exactly two entries:

    - ``system``: operating system + kernel release (e.g. ``Linux 5.15.0``);
    - ``shell``: the current shell (e.g. ``/bin/bash``).

    Key/value pairs in ``extra`` append to or override the defaults; entries whose value is
    ``None`` are ignored.
    """
    pairs: list[tuple[str, Any]] = [
        ("system", f"{platform.system()} {platform.release()}".strip()),
        ("shell", _current_shell()),
    ]

    if extra:
        overrides = {str(key): value for key, value in extra.items() if value is not None}
        merged: list[tuple[str, Any]] = []
        seen: set[str] = set()
        for key, value in pairs:
            if key in overrides:
                merged.append((key, overrides[key]))
                seen.add(key)
            else:
                merged.append((key, value))
        for key, value in overrides.items():
            if key not in seen:
                merged.append((key, value))
        pairs = merged

    return "\n".join(f"- {key}: {value}" for key, value in pairs)


# ----------------------------------------------------------------------
# Tool list
# ----------------------------------------------------------------------


def _tool_entry(value: Any) -> dict[str, Any]:
    """Normalize a single tool (``Tool`` / dict / str) into a uniform dict."""
    if isinstance(value, Mapping):
        return {
            "name": str(value.get("name") or value.get("id") or ""),
            "description": str(value.get("description") or ""),
            "enabled": bool(value.get("enabled", True)),
            "requires_approval": bool(value.get("requires_approval", False)),
        }
    if isinstance(value, str):
        return {"name": value, "description": "", "enabled": True, "requires_approval": False}
    return {
        "name": str(getattr(value, "name", "") or ""),
        "description": str(getattr(value, "description", "") or ""),
        "enabled": bool(getattr(value, "enabled", True)),
        "requires_approval": bool(getattr(value, "requires_approval", False)),
    }


def _iter_tools(tools: Any) -> list[Any]:
    """Normalize ``tools`` into a list of raw tool objects (registry / single / iterable)."""
    if tools is None:
        return []
    if isinstance(tools, (str, Mapping)):
        return [tools]
    if hasattr(tools, "tool_info") and callable(tools.tool_info):
        return list(tools.tool_info(include_disabled=True))
    if hasattr(tools, "all") and callable(tools.all):
        return list(tools.all())
    if hasattr(tools, "name"):  # a single Tool
        return [tools]
    return list(tools)


def render_tools(
    tools: Any,
    *,
    include_disabled: bool = False,
    empty_text: str = EMPTY_TOOLS_TEXT,
) -> str:
    """Render the tool list (a Markdown list) used to fill ``<tools_list>``.

    - ``tools`` accepts a ``ToolRegistry`` / ``Tool`` / dict / name string and their iterables;
    - only enabled tools are listed by default; tools that require approval
      (``requires_approval``) get a ``(requires approval)`` suffix;
    - returns ``empty_text`` when there is nothing to list.
    """
    entries = [_tool_entry(item) for item in _iter_tools(tools)]
    if not include_disabled:
        entries = [entry for entry in entries if entry["enabled"]]
    entries = [entry for entry in entries if entry["name"]]
    if not entries:
        return empty_text

    lines: list[str] = []
    for entry in entries:
        suffix = " (requires approval)" if entry["requires_approval"] else ""
        description = entry["description"].strip()
        line = f"- `{entry['name']}`{suffix}"
        if description:
            line += f": {description}"
        lines.append(line)
    return "\n".join(lines)


# ----------------------------------------------------------------------
# Skill list
# ----------------------------------------------------------------------


def _skill_entry(value: Any) -> dict[str, Any]:
    """Normalize a single skill (``SkillMetadata`` / dict / str) into a uniform dict."""
    if isinstance(value, Mapping):
        scope = value.get("scope")
        source = value.get("source")
        if scope is None and isinstance(source, Mapping):
            scope = source.get("scope")
        return {
            "name": str(value.get("name") or ""),
            "description": str(value.get("description") or ""),
            "scope": scope,
        }
    if isinstance(value, str):
        return {"name": value, "description": "", "scope": None}
    return {
        "name": str(getattr(value, "name", "") or ""),
        "description": str(getattr(value, "description", "") or ""),
        "scope": getattr(getattr(value, "source", None), "scope", None),
    }


def _iter_skills(skills: Any) -> list[Any]:
    """Normalize ``skills`` into a list of raw skill objects (registry / single / iterable)."""
    if skills is None:
        return []
    if isinstance(skills, (str, Mapping)):
        return [skills]
    if hasattr(skills, "list_skills") and callable(skills.list_skills):
        return list(skills.list_skills())
    if hasattr(skills, "name"):  # a single SkillMetadata
        return [skills]
    return list(skills)


def render_skills(
    skills: Any,
    *,
    include_scope: bool = True,
    empty_text: str = EMPTY_SKILLS_TEXT,
) -> str:
    """Render the skill list (a Markdown list) used to fill ``<skills_list>``.

    - ``skills`` accepts a ``SkillRegistry`` / ``SkillMetadata`` / dict / name string and their
      iterables;
    - with ``include_scope=True`` the source (``system`` / ``project``) is annotated;
    - returns ``empty_text`` when there is nothing to list.
    """
    entries = [_skill_entry(item) for item in _iter_skills(skills)]
    entries = [entry for entry in entries if entry["name"]]
    if not entries:
        return empty_text

    lines: list[str] = []
    for entry in entries:
        label = f" [{entry['scope']}]" if include_scope and entry["scope"] else ""
        description = entry["description"].strip()
        line = f"- `{entry['name']}`{label}"
        if description:
            line += f": {description}"
        lines.append(line)
    return "\n".join(lines)


# ----------------------------------------------------------------------
# Assembly
# ----------------------------------------------------------------------


def fill_placeholders(
    template: str,
    *,
    tools: str = "",
    skills: str = "",
    system_env: str = "",
) -> str:
    """Fill the three block texts into the template placeholders (placeholders absent from the
    template are ignored)."""
    text = template.replace(TOOLS_PLACEHOLDER, tools)
    text = text.replace(SKILLS_PLACEHOLDER, skills)
    text = text.replace(SYSTEM_ENV_PLACEHOLDER, system_env)
    return text


def build_system_prompt_parts(
    *,
    template: str | None = None,
    path: str | Path | None = None,
    tools: Any = None,
    skills: Any = None,
    system_env: str | None = None,
    extra_env: Mapping[str, Any] | None = None,
    include_disabled_tools: bool = False,
    include_skill_scope: bool = True,
    encoding: str = "utf-8",
) -> dict[str, str]:
    """Load the template and pre-fill it, returning each block's raw text plus the final prompt.

    The returned dict keys are ``template`` (raw template), ``tools``, ``skills``, ``system_env``
    and ``prompt`` (the fully filled prompt).

    Template source priority: ``template`` > ``path`` > built-in default template. When
    ``system_env`` is given explicitly the environment is not collected automatically.
    """
    if template is not None:
        template_text = template
    elif path is not None:
        template_text = load_template(path, encoding=encoding)
    else:
        template_text = load_default_template(encoding=encoding)

    tools_text = render_tools(tools, include_disabled=include_disabled_tools)
    skills_text = render_skills(skills, include_scope=include_skill_scope)
    if system_env is None:
        env_text = collect_system_env(extra=extra_env)
    else:
        env_text = str(system_env)

    prompt_text = fill_placeholders(
        template_text,
        tools=tools_text,
        skills=skills_text,
        system_env=env_text,
    )
    return {
        "template": template_text,
        "tools": tools_text,
        "skills": skills_text,
        "system_env": env_text,
        "prompt": prompt_text,
    }


def build_system_prompt(
    *,
    template: str | None = None,
    path: str | Path | None = None,
    tools: Any = None,
    skills: Any = None,
    system_env: str | None = None,
    extra_env: Mapping[str, Any] | None = None,
    include_disabled_tools: bool = False,
    include_skill_scope: bool = True,
    encoding: str = "utf-8",
) -> str:
    """Load the system prompt template, pre-fill tools / skills / user environment and return it.

    See ``build_system_prompt_parts()`` for the parameters; this function returns only the fully
    filled prompt text, which can be used directly as a system message.
    """
    return build_system_prompt_parts(
        template=template,
        path=path,
        tools=tools,
        skills=skills,
        system_env=system_env,
        extra_env=extra_env,
        include_disabled_tools=include_disabled_tools,
        include_skill_scope=include_skill_scope,
        encoding=encoding,
    )["prompt"]

