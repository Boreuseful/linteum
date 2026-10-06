"""``cli/subcommands/*`` 共用的 argparse 辅助函数。

本模块**不 import ``main``**（避免循环依赖）；``main`` 与各 parser builder 都从这里取共享
逻辑，保证顶层与子命令对同一组 flag 的口径一致。
"""

from __future__ import annotations

import argparse


def add_shared_flags(parser: argparse.ArgumentParser, *, inherited: bool = False) -> None:
    """添加顶层 / 子命令共享的 flag：``-w/--workspace``、``-s/--session``、``--config``、``-y``。

    ``inherited=True``（子命令侧）时默认值用 ``argparse.SUPPRESS``：属性只在命令行**显式给出**
    时才写入 namespace，因此 ``atom --workspace X chat`` 顶层解析出的 ``X`` 不会被 chat
    子 parser 的默认值覆盖（顶层与子命令共享同一个 namespace）。
    """
    suppress = argparse.SUPPRESS
    default = suppress if inherited else None

    parser.add_argument(
        "-w", "--workspace", metavar="PATH", default=default,
        help="工作区目录（默认命令启动的目录）")
    parser.add_argument(
        "-s", "--session", metavar="ID", default=default,
        help="会话 ID（默认按工作区自动查找）")
    parser.add_argument(
        "--config", metavar="PATH", default=default,
        help="配置文件路径（默认 ~/.agent/config.yaml）")

    if inherited:
        parser.add_argument(
            "-y", "--auto-approve", dest="auto_approve", action="store_true",
            default=suppress,
            help="自动批准需要授权的工具调用（如 bash）")
    else:
        parser.add_argument(
            "-y", "--auto-approve", dest="auto_approve", action="store_true",
            default=False,
            help="自动批准需要授权的工具调用（如 bash）")

    # 快照：只有 chat 会写；--use-snapshot 只读快照里的工作区。
    parser.add_argument(
        "--snapshot", dest="snapshot", action="store_true",
        default=(suppress if inherited else True),
        help="chat 运行后把模型配置 / config / 工作区写入快照（默认开启；仅 chat 写）")
    parser.add_argument(
        "--no-snapshot", dest="snapshot", action="store_false", default=suppress,
        help="本次 chat 运行不写快照")
    parser.add_argument(
        "--use-snapshot", dest="use_snapshot", action="store_true",
        default=(suppress if inherited else False),
        help="使用快照里保存的工作区（默认使用命令启动目录）")


def add_json_flag(parser: argparse.ArgumentParser, help: str) -> None:
    """添加 ``--json``（store_true），便于脚本消费命令输出。"""
    parser.add_argument("--json", action="store_true", help=help)


def group_dispatch(parser: argparse.ArgumentParser, handler, *, action_dest: str):
    """构造带子动作的组命令的分发函数（``set_defaults(func=...)`` 用）。

    - 未给出子动作（如 ``atom sessions``）→ 打印该组 ``--help`` 并返回 0，而不是静默退出；
    - 给出子动作 → 交给注入的 ``handler``。

    与 hermes 的 ``_dispatch_sessions`` 同一模式：parser 通过闭包注入，handler 仍是
    由 ``main`` 依赖注入进来的那个，因此本模块依旧不 import ``main``。
    """

    def _dispatch(args):
        if getattr(args, action_dest, None) is None:
            parser.print_help()
            return 0
        return handler(args)

    return _dispatch
