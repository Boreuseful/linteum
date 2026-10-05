"""``atom tools`` 子命令 parser。"""

from __future__ import annotations

from typing import Callable

from cli.subcommands._shared import add_json_flag, add_shared_flags, group_dispatch


def build_tools_parser(subparsers, *, cmd_tools: Callable) -> None:
    """把 ``tools`` 子命令（含 list）挂到 ``subparsers`` 上。"""
    parser = subparsers.add_parser(
        "tools", help="工具列表",
        description="列出内置工具及其启用状态。")
    sub = parser.add_subparsers(dest="tools_action")

    lst = sub.add_parser("list", help="列出工具")
    add_shared_flags(lst, inherited=True)
    lst.add_argument(
        "--bash", dest="enable_bash", action="store_true", default=False,
        help="包含 bash 工具（默认不含）")
    add_json_flag(lst, "以 JSON 输出")
    lst.set_defaults(func=cmd_tools)

    parser.set_defaults(func=group_dispatch(parser, cmd_tools, action_dest="tools_action"))
