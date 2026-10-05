"""``atom sessions`` 子命令 parser。"""

from __future__ import annotations

from typing import Callable

from cli.subcommands._shared import add_json_flag, add_shared_flags, group_dispatch


def build_sessions_parser(subparsers, *, cmd_sessions: Callable) -> None:
    """把 ``sessions`` 子命令（list / show / new / switch / current）挂到 ``subparsers`` 上。"""
    parser = subparsers.add_parser(
        "sessions", help="会话历史（list / show / new / switch / current）",
        description="查看、创建与切换工作区与项目根目录下 .agent/sessions 里的会话。")
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

    new = sub.add_parser("new", help="创建新会话（并设为当前会话）")
    add_shared_flags(new, inherited=True)
    new.add_argument(
        "--id", dest="session_id", default=None, metavar="ID",
        help="指定会话 ID（默认生成 uuid4）")
    new.add_argument(
        "--no-switch", dest="switch", action="store_false", default=True,
        help="只创建，不设为当前会话")
    new.add_argument(
        "--force", action="store_true", default=False,
        help="同名会话已存在时用全新会话覆盖它")
    add_json_flag(new, "以 JSON 输出")
    new.set_defaults(func=cmd_sessions)

    switch = sub.add_parser("switch", help="切换当前会话（会话必须已存在）")
    add_shared_flags(switch, inherited=True)
    switch.add_argument("session_id", help="会话 ID（唯一前缀即可）")
    switch.set_defaults(func=cmd_sessions)

    current = sub.add_parser("current", help="显示当前会话")
    add_shared_flags(current, inherited=True)
    add_json_flag(current, "以 JSON 输出")
    current.set_defaults(func=cmd_sessions)

    parser.set_defaults(func=group_dispatch(parser, cmd_sessions, action_dest="sessions_action"))
