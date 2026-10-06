"""``linteum`` 命令行入口：注册子命令、解析 ``argv``、分发到 handler。

架构（对齐 hermes 的 Builder + 依赖注入）:

- ``cli/subcommands/<group>.py`` **只**提供
  ``build_<group>_parser(subparsers, *, cmd_<group>=...)``，handler 由本模块注入；
  这些模块**绝不 import main**（避免循环依赖）。
- ``_forward_command()`` 是**惰性 import 工厂**：handler 在被调用时才
  ``importlib.import_module(target)``，因此
  (a) 快路径不为重模块付启动成本；(b) ``patch("<module>.<attr>")`` 仍能拦截。
- ``_build_cli_parser()`` 逐个 ``build_<group>_parser(subparsers, cmd_<group>=handler)``，
  **注册顺序即 ``linteum --help`` 顺序**。
- 统一分发契约：每个叶子 parser ``set_defaults(func=handler)``，``main()`` 统一
  ``args.func(args)``；handler 返回 ``int`` 时作为进程退出码（``None`` = 成功）。

同时支持 ``python -m cli.main`` 与 ``python cli/main.py``。
"""

from __future__ import annotations

import os
import sys

# 允许 `python cli/main.py` 直接运行：确保项目根目录在 sys.path 上，这样包内绝对导入
# `cli.*` 与 `linteum_agent.*` 都能解析（`python -m cli.main` 时无副作用）。
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from cli import PROG, __version__, snapshot  # noqa: E402 - 依赖上面的 sys.path 处理
from cli._parser import build_top_level_parser  # noqa: E402
from cli.subcommands.chat import build_chat_parser  # noqa: E402
from cli.subcommands.config import build_config_parser  # noqa: E402
from cli.subcommands.model import build_model_parser  # noqa: E402
from cli.subcommands.sessions import build_sessions_parser  # noqa: E402
from cli.subcommands.setup import build_setup_parser  # noqa: E402
from cli.subcommands.skills import build_skills_parser  # noqa: E402
from cli.subcommands.snapshot import build_snapshot_parser  # noqa: E402
from cli.subcommands.status import build_status_parser  # noqa: E402
from cli.subcommands.tools import build_tools_parser  # noqa: E402

# ----------------------------------------------------------------------
# 惰性 import 工厂
# ----------------------------------------------------------------------


def _forward_command(name: str, module: str, attr: str, *, forward_return: bool = False,
                     doc: str = ""):
    """返回一个 ``linteum <cmd>`` handler：调用时才把 ``args`` 交给 ``<module>.<attr>``。

    ``importlib.import_module`` 放在**调用时**，因此
    (a) 启动 / ``--help`` 不为实现模块付 import 成本；
    (b) ``patch("<module>.<attr>")`` 仍能拦截真实实现。
    ``forward_return=True`` 时把实现返回码上浮给 ``main()`` 作为进程退出码。
    """

    def _cmd(args):
        import importlib

        result = getattr(importlib.import_module(module), attr)(args)
        return result if forward_return else None

    _cmd.__name__ = _cmd.__qualname__ = name
    _cmd.__doc__ = doc or None
    return _cmd


# handler 由 main 唯一定义并注入 subcommand builder；实现本体在 cli.commands.*（惰性 import）。
cmd_chat = _forward_command(
    "cmd_chat", "cli.commands.chat", "run_chat", forward_return=True, doc="与 Agent 对话。")
cmd_setup = _forward_command(
    "cmd_setup", "cli.commands.setup", "run_setup", forward_return=True, doc="初始化配置与项目状态。")
cmd_snapshot = _forward_command(
    "cmd_snapshot", "cli.commands.snapshot", "run_snapshot", forward_return=True, doc="快照管理。")
cmd_sessions = _forward_command(
    "cmd_sessions", "cli.commands.sessions", "run_sessions", forward_return=True, doc="会话历史。")
cmd_config = _forward_command(
    "cmd_config", "cli.commands.config", "run_config", forward_return=True, doc="配置读写。")
cmd_model = _forward_command(
    "cmd_model", "cli.commands.model", "run_model", forward_return=True, doc="模型选择。")
cmd_tools = _forward_command(
    "cmd_tools", "cli.commands.tools", "run_tools", forward_return=True, doc="工具列表。")
cmd_skills = _forward_command(
    "cmd_skills", "cli.commands.skills", "run_skills", forward_return=True, doc="Skill 列表。")
cmd_status = _forward_command(
    "cmd_status", "cli.commands.status", "run_status", forward_return=True, doc="运行时状态。")


# ----------------------------------------------------------------------
# 子命令清单 / 默认命令
# ----------------------------------------------------------------------

#: 全部内置子命令名（与 ``_build_cli_parser`` 的注册顺序 / 集合严格一致）。
_BUILTIN_SUBCOMMANDS = frozenset(
    {"chat", "setup", "snapshot", "sessions", "config", "model", "tools", "skills", "status"})

#: 无子命令时落到 ``chat``；这些是 chat 专属字段的默认值。
_CHAT_DEFAULTS = {
    "prompt": None,
    "stream": True,
    "new": False,
    "enable_bash": False,
    "max_tool_rounds": 20,
    "json": False,
}


def _default_to_chat(args):
    """``linteum``（无子命令）→ 走 ``cmd_chat``，补上未解析的 chat 默认值。"""
    args.command = "chat"
    for name, default in _CHAT_DEFAULTS.items():
        if not hasattr(args, name):
            setattr(args, name, default)
    return cmd_chat(args)


# ----------------------------------------------------------------------
# 解析树
# ----------------------------------------------------------------------


def _build_cli_parser():
    """构建完整 ``linteum`` argparse 树 -> ``(parser, subparsers)``。

    注册顺序即 ``linteum --help`` 顺序，保持稳定。每个组的 parser 在
    ``cli/subcommands/<group>.py`` 里构建，handler 在这里注入，因此那些模块从不 import main。
    """
    parser, subparsers = build_top_level_parser()

    build_chat_parser(subparsers, cmd_chat=cmd_chat)
    build_setup_parser(subparsers, cmd_setup=cmd_setup)
    build_snapshot_parser(subparsers, cmd_snapshot=cmd_snapshot)
    build_sessions_parser(subparsers, cmd_sessions=cmd_sessions)
    build_config_parser(subparsers, cmd_config=cmd_config)
    build_model_parser(subparsers, cmd_model=cmd_model)
    build_tools_parser(subparsers, cmd_tools=cmd_tools)
    build_skills_parser(subparsers, cmd_skills=cmd_skills)
    build_status_parser(subparsers, cmd_status=cmd_status)

    return parser, subparsers


def _parse_cli_args(parser, subparsers, argv):
    """解析 ``argv``；子命令可省略（裸 ``linteum`` 落到 chat），因此 subparsers 非必需。"""
    subparsers.required = False
    return parser.parse_args(argv)


def _cmd_version() -> None:
    print(f"{PROG} {__version__}")


def _run(func, args) -> int:
    """执行 handler：``BrokenPipeError``（如输出被 ``head`` 提前关闭）静默退出。"""
    try:
        result = func(args)
    except BrokenPipeError:
        # 把 stdout 换成 devnull，避免解释器退出时再次 flush 抛栈。
        try:
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, sys.stdout.fileno())
        except OSError:  # pragma: no cover - 兜底
            pass
        return 0
    return result if isinstance(result, int) else 0


def main(argv=None) -> int:
    """CLI 主入口：解析 -> 处理全局 flag -> 分发到 ``args.func``，返回进程退出码。"""
    argv = list(sys.argv[1:] if argv is None else argv)

    parser, subparsers = _build_cli_parser()
    args = _parse_cli_args(parser, subparsers, argv)

    if getattr(args, "version", False):
        _cmd_version()
        return 0

    # 快照（默认启用）：解析工作区（默认命令启动目录 / --use-snapshot）+ 写快照。
    # setup / snapshot 子命令自行管理，boot() 会跳过。
    snapshot.boot(args)

    # 没有子命令（可带顶层 -w / -s / -y）→ 默认 chat；`linteum` 直接进入交互式对话。
    if args.command is None:
        return _run(_default_to_chat, args)

    func = getattr(args, "func", None)
    if func is None:
        parser.print_help()
        return 0

    return _run(func, args)


if __name__ == "__main__":
    sys.exit(main())

