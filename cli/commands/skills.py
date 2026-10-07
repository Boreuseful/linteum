"""Implementation of ``linteum skills``: discover system / project Skills (offline, no Agent construction)."""

from __future__ import annotations

import json
import sys
from pathlib import Path


def run_skills(args) -> int:
    if getattr(args, "skills_action", None) != "list":
        print("Usage: linteum skills list ...", file=sys.stderr)
        return 2

    from linteum_agent.skills import SkillRegistry

    workspace = Path(getattr(args, "workspace", None) or Path.cwd()).expanduser()
    registry = SkillRegistry(
        system_skill_dirs=[Path.home() / ".agent" / "skills"],
        project_skill_dirs=[workspace / ".agent" / "skills"],
    )
    registry.discover()
    skills = registry.list_skills()

    if getattr(args, "json", False):
        print(json.dumps(
            [
                {
                    "name": skill.name,
                    "description": skill.description,
                    "scope": skill.source.scope,
                    "path": str(skill.path),
                }
                for skill in skills
            ],
            ensure_ascii=False, indent=2, default=str,
        ))
        return 0
    if not skills:
        print("(no Skills discovered)")
        return 0
    for skill in skills:
        print(f"{skill.name:<24} [{skill.source.scope}] {skill.description}")
    return 0
