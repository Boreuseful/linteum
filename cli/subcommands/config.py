"""``atom config`` 子命令 parser。"""

from __future__ import annotations

from typing import Callable

from cli.subcommands._shared import add_shared_flags, group_dispatch


def build_config_parser(subparsers, *, cmd_config: Callable) -> None:
    """把 ``config`` 子命令（含 path / show / get / set）挂到 ``subparsers`` 上。"""
    parser = subparsers.add_parser(
        "config", help="查看 / 修改配置",
        description="读写 config.yaml；set 目前支持 llm.<field>。")
    sub = parser.add_subparsers(dest="config_action")

    p_path = sub.add_parser("path", help="打印配置文件路径")
    add_shared_flags(p_path, inherited=True)
    p_path.set_defaults(func=cmd_config)

    p_show = sub.add_parser("show", help="打印当前配置（YAML）")
    add_shared_flags(p_show, inherited=True)
    p_show.set_defaults(func=cmd_config)

    p_get = sub.add_parser("get", help="读取一个配置项（点号路径，如 llm.model）")
    add_shared_flags(p_get, inherited=True)
    p_get.add_argument("key", help="点号路径，如 llm.model / llm.provider / version")
    p_get.set_defaults(func=cmd_config)

    p_set = sub.add_parser(
        "set", help="写入一个 llm 配置项（如 config set llm.model gpt-4o）")
    add_shared_flags(p_set, inherited=True)
    p_set.add_argument("key", help="点号路径，目前支持 llm.<field>")
    p_set.add_argument("value", help="新值（数值 / 布尔会按字段类型规范化）")
    p_set.set_defaults(func=cmd_config)

    parser.set_defaults(func=group_dispatch(parser, cmd_config, action_dest="config_action"))
