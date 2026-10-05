"""``atom status`` 子命令 parser。"""

from __future__ import annotations

from typing import Callable

from cli.subcommands._shared import add_json_flag, add_shared_flags


def build_status_parser(subparsers, *, cmd_status: Callable) -> None:
    """把 ``status`` 子命令挂到 ``subparsers`` 上。"""
    parser = subparsers.add_parser(
        "status", help="查看运行时状态",
        description="打印工作区、配置、会话数量等运行时信息。")
    add_shared_flags(parser, inherited=True)
    add_json_flag(parser, "以 JSON 输出")
    parser.set_defaults(func=cmd_status)
