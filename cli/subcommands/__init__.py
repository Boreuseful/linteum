"""``cli/subcommands/*`` -- a collection of pure parser builders.

Each ``<group>.py`` only exports ``build_<group>_parser(subparsers, *, cmd_<group>=...)``: it calls
``add_parser`` + ``add_argument`` + ``set_defaults(func=cmd_<group>)`` here, **injecting** the
handler while **never importing main** itself (to avoid circular imports). Shared argparse helper
functions live in ``_shared.py``. ``main._build_cli_parser`` calls them one by one, and **the
registration order is the ``linteum --help`` order**.
"""

from __future__ import annotations
