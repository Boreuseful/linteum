"""Parser for the ``linteum setup`` subcommand."""

from __future__ import annotations

from typing import Callable

from cli.subcommands._shared import add_shared_flags


def build_setup_parser(subparsers, *, cmd_setup: Callable) -> None:
    """Attach the ``setup`` subcommand to ``subparsers``."""
    parser = subparsers.add_parser(
        "setup",
        help="Initialize config and project state",
        description="Initialize ~/.agent/config.yaml (future settings are initialized here too). "
                    "setup does not write the snapshot -- the snapshot is only updated by chat.")
    add_shared_flags(parser, inherited=True)
    parser.add_argument(
        "--force", action="store_true", default=False,
        help="Overwrite the config if it already exists (keeps the original file by default)")
    parser.add_argument(
        "--print", dest="print_only", action="store_true", default=False,
        help="Print the default config to stdout only, without writing any file")
    parser.set_defaults(func=cmd_setup)
