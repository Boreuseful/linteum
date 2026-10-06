"""顶层 argparse 构造。

本模块只负责**顶层 parser**（``prog`` / 顶层 flags / subparsers 动作）与一个自定义
``ArgumentParser``；除 ``chat``（默认命令，由 ``subcommands/chat.py`` 构建）外的每个子命令
parser 都由 ``cli/subcommands/<group>.py`` 提供，``main._build_cli_parser`` 逐个调用
``build_<group>_parser(subparsers, ...)`` 并注入 handler。
"""

from __future__ import annotations

import argparse
import difflib

from cli import PROG
from cli.subcommands._shared import add_shared_flags

#: ``linteum --help`` 结尾的用法示例（与注册顺序一起构成用户可见的命令面）。
_EPILOGUE = """\
示例:
  linteum                                 进入交互式对话（默认写快照）
  linteum setup                           初始化 ~/.agent/config.yaml
  linteum chat -p "总结这个仓库"           单次问答
  linteum chat --bash -y                  启用 bash 工具并自动授权
  linteum --use-snapshot                  用快照里保存的工作区启动
  linteum snapshot show                   查看快照（模型配置 + config + 工作区）
  linteum sessions list                   列出工作区里的会话
  linteum status                          查看运行时状态
"""


class CliArgumentParser(argparse.ArgumentParser):
    """未知子命令时给出三行错误 + 最近似建议，而不是打印整棵命令树。

    原生 argparse 在首个 positional 不是已注册子命令时，会打印完整 usage + 巨长的
    ``(choose from '...', ...)``，把唯一有用的信息（这个词不是命令）淹没掉。这里把它改成
    三行 + ``difflib`` 的 "Did you mean"；其余错误仍沿用 argparse 原生形态。
    """

    def _check_value(self, action, value):  # noqa: D401 - 覆写 argparse 钩子
        if isinstance(action, argparse._SubParsersAction) and value not in action.choices:
            lines = [f"{self.prog}: '{value}' is not a `{self.prog}` command."]
            close = difflib.get_close_matches(str(value), list(action.choices), n=3, cutoff=0.6)
            if close:
                lines.append(f"Did you mean: {', '.join(close)}?")
            lines.append(f"Run `{self.prog} --help` to see all commands.")
            self.exit(2, "\n".join(lines) + "\n")
        super()._check_value(action, value)


def build_top_level_parser() -> tuple[argparse.ArgumentParser, argparse._SubParsersAction]:
    """构造顶层 parser 与其 ``subparsers`` 动作。

    返回 ``(parser, subparsers)``；调用方负责依次 ``subparsers.add_parser(...)`` 注册子命令，
    并给叶子 parser ``set_defaults(func=...)``。
    """
    parser = CliArgumentParser(
        prog=PROG,
        description="linteum_agent —— 带工具调用能力的命令行 Agent",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=_EPILOGUE,
    )
    _add_top_level_flags(parser)
    # metavar=<command> 让 usage 行保持 ``linteum [...] <command>``，而不是把全部子命令名铺开；
    # ``linteum --help`` 仍会逐条列出每个命令。
    subparsers = parser.add_subparsers(dest="command", help="要运行的子命令", metavar="<command>")
    return parser, subparsers


def _add_top_level_flags(parser: argparse.ArgumentParser) -> None:
    """顶层 flags：``--version`` + 与子命令共享的工作区/会话/配置/授权 flag。"""
    parser.add_argument(
        "--version", action="store_true", help="打印版本并退出")
    # 工作区 / 会话 / 配置 / 自动授权：既出现在顶层，也由子命令继承（见 _shared.add_shared_flags）。
    add_shared_flags(parser, inherited=False)
