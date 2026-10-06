"""``linteum model`` 子命令 parser。"""

from __future__ import annotations

from typing import Callable

from cli.subcommands._shared import add_shared_flags, group_dispatch


def build_model_parser(subparsers, *, cmd_model: Callable) -> None:
    """把 ``model`` 子命令（含 show / set）挂到 ``subparsers`` 上。"""
    parser = subparsers.add_parser(
        "model", help="查看 / 切换默认模型",
        description="读写 config.yaml 里的 llm.model（与 llm.provider）。")
    sub = parser.add_subparsers(dest="model_action")

    p_show = sub.add_parser("show", help="显示当前 provider 与 model")
    add_shared_flags(p_show, inherited=True)
    p_show.set_defaults(func=cmd_model)

    p_set = sub.add_parser("set", help="设置默认模型")
    add_shared_flags(p_set, inherited=True)
    p_set.add_argument("name", help="模型名，如 gpt-4o")
    p_set.add_argument("--provider", default=None, help="同时切换 provider（如 deepseek）")
    p_set.set_defaults(func=cmd_model)

    parser.set_defaults(func=group_dispatch(parser, cmd_model, action_dest="model_action"))
