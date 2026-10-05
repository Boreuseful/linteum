"""``atom snapshot`` 子命令 parser。"""

from __future__ import annotations

from typing import Callable

from cli.subcommands._shared import add_json_flag, add_shared_flags, group_dispatch


def build_snapshot_parser(subparsers, *, cmd_snapshot: Callable) -> None:
    """把 ``snapshot`` 子命令（含 show / save / clear / path）挂到 ``subparsers`` 上。"""
    parser = subparsers.add_parser(
        "snapshot", help="快照（show / save / clear / path）",
        description="查看 / 写入 / 清除快照（模型配置 + config + 工作区）。")
    sub = parser.add_subparsers(dest="snapshot_action")

    show = sub.add_parser("show", help="显示当前快照")
    add_shared_flags(show, inherited=True)
    add_json_flag(show, "以 JSON 输出完整快照")
    show.set_defaults(func=cmd_snapshot)

    save = sub.add_parser("save", help="立即写入快照")
    add_shared_flags(save, inherited=True)
    save.set_defaults(func=cmd_snapshot)

    clear = sub.add_parser("clear", help="删除快照文件")
    add_shared_flags(clear, inherited=True)
    clear.set_defaults(func=cmd_snapshot)

    path = sub.add_parser("path", help="打印快照文件路径")
    add_shared_flags(path, inherited=True)
    path.set_defaults(func=cmd_snapshot)

    parser.set_defaults(func=group_dispatch(parser, cmd_snapshot, action_dest="snapshot_action"))
