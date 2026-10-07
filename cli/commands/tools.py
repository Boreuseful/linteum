"""Implementation of ``linteum tools``: list built-in tools (offline, no Agent construction)."""

from __future__ import annotations

import json
import sys
from pathlib import Path


def run_tools(args) -> int:
    if getattr(args, "tools_action", None) != "list":
        print("Usage: linteum tools list ...", file=sys.stderr)
        return 2

    from linteum_agent.tools.builtin import create_builtin_tools

    workspace = getattr(args, "workspace", None) or str(Path.cwd())
    registry = create_builtin_tools(
        include_bash=bool(getattr(args, "enable_bash", False)),
        workspace=workspace,
    )
    infos = registry.tool_info()

    if getattr(args, "json", False):
        print(json.dumps(infos, ensure_ascii=False, indent=2, default=str))
        return 0
    if not infos:
        print("(no tools available)")
        return 0
    for info in infos:
        state = "enabled " if info.get("enabled") else "disabled"
        approval = " (requires approval)" if info.get("requires_approval") else ""
        print(f"{info['name']:<16} {state}{approval}  {info.get('description', '')}")
    return 0
