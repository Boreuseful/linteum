"""``atom setup`` 子命令 parser。"""

from __future__ import annotations

from typing import Callable

from cli.subcommands._shared import add_shared_flags


def build_setup_parser(subparsers, *, cmd_setup: Callable) -> None:
    """把 ``setup`` 子命令挂到 ``subparsers`` 上。"""
    parser = subparsers.add_parser(
        "setup",
        help="初始化配置与项目状态",
        description="初始化 <项目根目录>/.agent/config.yaml（未来新增的设置也在这里初始化）。"
                    "setup 不写快照 —— 快照只由 chat 更新。")
    add_shared_flags(parser, inherited=True)
    parser.add_argument(
        "--force", action="store_true", default=False,
        help="配置已存在时覆盖它（默认保留原文件）")
    parser.add_argument(
        "--print", dest="print_only", action="store_true", default=False,
        help="只把默认配置打印到 stdout，不写任何文件")
    parser.set_defaults(func=cmd_setup)
