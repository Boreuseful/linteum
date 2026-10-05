"""``atom skills`` 子命令 parser。"""

from __future__ import annotations

from typing import Callable

from cli.subcommands._shared import add_json_flag, add_shared_flags, group_dispatch


def build_skills_parser(subparsers, *, cmd_skills: Callable) -> None:
    """把 ``skills`` 子命令（含 list）挂到 ``subparsers`` 上。"""
    parser = subparsers.add_parser(
        "skills", help="Skill 列表",
        description="发现系统（~/.agent/skills）与项目（<workspace>/.agent/skills）Skill。")
    sub = parser.add_subparsers(dest="skills_action")

    lst = sub.add_parser("list", help="列出发现的 Skill")
    add_shared_flags(lst, inherited=True)
    add_json_flag(lst, "以 JSON 输出")
    lst.set_defaults(func=cmd_skills)

    parser.set_defaults(func=group_dispatch(parser, cmd_skills, action_dest="skills_action"))
