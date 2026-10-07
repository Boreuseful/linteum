"""Parser for the ``linteum skills`` subcommand."""

from __future__ import annotations

from typing import Callable

from cli.subcommands._shared import add_json_flag, add_shared_flags, group_dispatch


def build_skills_parser(subparsers, *, cmd_skills: Callable) -> None:
    """Attach the ``skills`` subcommand (including list) to ``subparsers``."""
    parser = subparsers.add_parser(
        "skills", help="Skill list",
        description="Discover system (~/.agent/skills) and project (<workspace>/.agent/skills) Skills.")
    sub = parser.add_subparsers(dest="skills_action")

    lst = sub.add_parser("list", help="List discovered Skills")
    add_shared_flags(lst, inherited=True)
    add_json_flag(lst, "Output as JSON")
    lst.set_defaults(func=cmd_skills)

    parser.set_defaults(func=group_dispatch(parser, cmd_skills, action_dest="skills_action"))
