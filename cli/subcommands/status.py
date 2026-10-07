"""Parser for the ``linteum status`` subcommand."""

from __future__ import annotations

from typing import Callable

from cli.subcommands._shared import add_json_flag, add_shared_flags


def build_status_parser(subparsers, *, cmd_status: Callable) -> None:
    """Attach the ``status`` subcommand to ``subparsers``."""
    parser = subparsers.add_parser(
        "status", help="View runtime status",
        description="Print runtime information such as workspace, config, and session count.")
    add_shared_flags(parser, inherited=True)
    add_json_flag(parser, "Output as JSON")
    parser.set_defaults(func=cmd_status)
