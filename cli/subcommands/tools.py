"""Parser for the ``linteum tools`` subcommand."""

from __future__ import annotations

from typing import Callable

from cli.subcommands._shared import add_json_flag, add_shared_flags, group_dispatch


def build_tools_parser(subparsers, *, cmd_tools: Callable) -> None:
    """Attach the ``tools`` subcommand (including list) to ``subparsers``."""
    parser = subparsers.add_parser(
        "tools", help="Tool list",
        description="List the built-in tools and their enabled state.")
    sub = parser.add_subparsers(dest="tools_action")

    lst = sub.add_parser("list", help="List tools")
    add_shared_flags(lst, inherited=True)
    lst.add_argument(
        "--bash", dest="enable_bash", action="store_true", default=False,
        help="Include the bash tool (excluded by default)")
    add_json_flag(lst, "Output as JSON")
    lst.set_defaults(func=cmd_tools)

    parser.set_defaults(func=group_dispatch(parser, cmd_tools, action_dest="tools_action"))
