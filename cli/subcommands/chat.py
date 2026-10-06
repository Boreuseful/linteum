"""``linteum chat`` 子命令 parser（也是无子命令时的默认命令）。"""

from __future__ import annotations

from typing import Callable

from cli.subcommands._shared import add_shared_flags


def build_chat_parser(subparsers, *, cmd_chat: Callable) -> None:
    """把 ``chat`` 子命令挂到 ``subparsers`` 上。"""
    parser = subparsers.add_parser(
        "chat",
        help="与 Agent 对话（交互式 REPL 或单次问答）",
        description="启动一次 Agent 会话：省略 --prompt 进入交互式 REPL。",
    )
    # 工作区 / 会话 / 配置 / 自动授权：与顶层同名，用 SUPPRESS 默认值以保留顶层解析结果。
    add_shared_flags(parser, inherited=True)
    parser.add_argument(
        "-p", "--prompt", metavar="TEXT", default=None,
        help="单次问答的提示词；省略则进入交互式 REPL")
    parser.add_argument(
        "--stream", dest="stream", action="store_true", default=True,
        help="流式输出（默认开启）")
    parser.add_argument(
        "--no-stream", dest="stream", action="store_false",
        help="关闭流式输出，等待整段回复")
    parser.add_argument(
        "--new", action="store_true", default=False,
        help="强制开始新会话（忽略工作区里已存在的上下文）")
    parser.add_argument(
        "--bash", dest="enable_bash", action="store_true", default=False,
        help="启用 bash 工具（执行前需授权，除非 --auto-approve）")
    parser.add_argument(
        "--max-tool-rounds", dest="max_tool_rounds", type=int, default=20, metavar="N",
        help="单次请求内工具调用的最大轮数（默认 20）")
    parser.add_argument(
        "--json", action="store_true", default=False,
        help="以 JSON 输出单次问答的结果（便于脚本消费）")
    parser.set_defaults(func=cmd_chat)
