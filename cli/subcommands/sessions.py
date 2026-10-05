"""``atom sessions`` 子命令 parser。"""

from __future__ import annotations

from typing import Callable

from cli.subcommands._shared import add_json_flag, add_shared_flags, group_dispatch


def build_sessions_parser(subparsers, *, cmd_sessions: Callable) -> None:
    """把 ``sessions`` 子命令（含 ``list`` / ``show``）挂到 ``subparsers`` 上。"""
    parser = subparsers.add_parser(
        "sessions", help="会话历史（list / show）",
        description="查看工作区与项目根目录下 .agent/sessions 里的会话。")
    sub = parser.add_subparsers(dest="sessions_action")

    lst = sub.add_parser("list", help="列出会话摘要")
    add_shared_flags(lst, inherited=True)
    lst.add_argument(
        "--workspace-only", dest="workspace_only", action="store_true",
        help="只列工作区目录里的会话，不含项目根目录")
    lst.add_argument(
        "--current-workspace", dest="current_workspace", action="store_true",
        help="只列与当前工作区匹配的会话")
    add_json_flag(lst, "以 JSON 输出会话摘要")
    lst.set_defaults(func=cmd_sessions)

    show = sub.add_parser("show", help="查看某个会话的消息与用量")
    add_shared_flags(show, inherited=True)
    show.add_argument("session_id", help="会话 ID（唯一前缀即可）")
    add_json_flag(show, "以 JSON 输出")
    show.set_defaults(func=cmd_sessions)

    parser.set_defaults(func=group_dispatch(parser, cmd_sessions, action_dest="sessions_action"))
