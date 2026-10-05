"""atom_agent 命令行前端包。

对外入口（二者等价）::

    python -m cli.main <command> ...
    python -m cli <command> ...

架构（对齐 hermes 的 Builder + 依赖注入）:

- ``main.py``：解析 ``argv``、构建完整 argparse 树、按 ``args.func(args)`` 分发；
- ``subcommands/<group>.py``：**只**提供 ``build_<group>_parser(subparsers, ...)``，
  由 ``main`` 注入 handler，绝不 import ``main``（避免循环依赖）；
- ``commands/<group>.py``：各子命令的实现，由 ``_forward_command`` 在**调用时**惰性
  import（``importlib.import_module``），兼顾启动速度与 monkeypatch 可拦截性。
"""

from __future__ import annotations

#: 命令行程序名（``argparse`` 的 ``prog``，也是 ``--help`` / 用法行的前缀）。
PROG = "atom"

#: CLI 版本号。
__version__ = "0.1.0"

__all__ = ["PROG", "__version__"]
