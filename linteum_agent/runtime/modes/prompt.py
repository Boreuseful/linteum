"""Prompt rendering and the host-rendered environment block (§8.3/§8.4)."""

from __future__ import annotations

import re
import sys
import unicodedata
from typing import Mapping

from ..tools.workspace import Workspace
from .errors import ModeTemplate

_VAR_PATTERN = re.compile(r"\{\{\s*([a-z_][a-z0-9_]*)\s*\}\}")
_OPEN_ESCAPE = ""
_CLOSE_ESCAPE = ""

#: Fixed variables accepted in a mode body.  Anything else is an error.
TEMPLATE_VARIABLES = frozenset(
    {
        "workspace",
        "workspace_name",
        "read_only",
        "mode",
        "mode_description",
        "platform",
        "shell",
    }
)


def safe_inline(value: object, *, limit: int = 512) -> str:
    """Make one value safe to interpolate into a system block.

    CR/LF/TAB and other control characters become ``?``, backticks become
    ``'`` (so a value cannot terminate a code span), and the result is
    truncated to ``limit`` characters.
    """
    text = str(value)
    result: list[str] = []
    for char in text:
        if char in "\r\n\t" or unicodedata.category(char).startswith("C"):
            result.append("?")
        elif char == "`":
            result.append("'")
        else:
            result.append(char)
    return "".join(result)[:limit]


def render_prompt(body: str, variables: Mapping[str, str]) -> str:
    """Render the strict ``{{variable}}`` syntax.

    ``\\{{`` / ``\\}}`` produce literal braces.  Unknown variables fail the
    mode (``E_MODE_TEMPLATE``) instead of silently leaking the token.
    """
    if not isinstance(body, str):
        raise ModeTemplate("mode body must be text")
    if not isinstance(variables, Mapping):
        raise ModeTemplate("mode template variables must be a mapping")

    protected = body.replace(r"\{{", _OPEN_ESCAPE).replace(
        r"\}}", _CLOSE_ESCAPE
    )

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in TEMPLATE_VARIABLES:
            raise ModeTemplate(
                f"unknown template variable '{{{{{name}}}}}'",
                key="prompt",
                context={"variable": name},
            )
        if name not in variables:
            raise ModeTemplate(
                f"template variable '{{{{{name}}}}}' has no value",
                key="prompt",
                context={"variable": name},
            )
        return safe_inline(variables[name])

    rendered = _VAR_PATTERN.sub(replace, protected)
    return rendered.replace(_OPEN_ESCAPE, "{{").replace(_CLOSE_ESCAPE, "}}")


def template_variables(
    *,
    mode_name: str,
    description: str,
    workspace: Workspace,
    shell_available: bool,
) -> dict[str, str]:
    """Build the fixed variable map for one mode render."""
    root = workspace.root
    return {
        "workspace": str(root),
        "workspace_name": root.name,
        "read_only": "true" if workspace.read_only else "false",
        "mode": mode_name,
        "mode_description": description,
        "platform": sys.platform,
        "shell": "bash" if shell_available else "none",
    }


def render_environment_block(
    *,
    name: str,
    description: str,
    workspace: Workspace,
    shell_available: bool,
) -> str:
    """Render the always-present Environment block (§8.3)."""
    mode_line = f"- Mode: `{safe_inline(name)}`"
    if description:
        mode_line += f" — {safe_inline(description)}"
    lines = [
        "# Environment",
        "",
        mode_line,
        f"- Workspace root: `{safe_inline(workspace.root)}`",
        f"- Workspace access: {'read-only' if workspace.read_only else 'read-write'}",
        f"- Platform: `{safe_inline(sys.platform)}`",
        f"- Shell: `{'bash' if shell_available else 'none'}`",
    ]
    return "\n".join(lines)


__all__ = [
    "TEMPLATE_VARIABLES",
    "render_environment_block",
    "render_prompt",
    "safe_inline",
    "template_variables",
]
