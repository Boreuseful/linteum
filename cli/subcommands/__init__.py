"""``cli/subcommands/*`` —— 纯 parser builder 集合。

每个 ``<group>.py`` 只导出 ``build_<group>_parser(subparsers, *, cmd_<group>=...)``：
在本模块里 ``add_parser`` + ``add_argument`` + ``set_defaults(func=cmd_<group>)``，把 handler
**注入**进来，自己**绝不 import main**（避免循环依赖）。共享的 argparse 辅助函数在
``_shared.py``。``main._build_cli_parser`` 逐个调用它们，**注册顺序即 ``linteum --help`` 顺序**。
"""

from __future__ import annotations
