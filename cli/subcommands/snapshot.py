"""Parser for the ``linteum snapshot`` subcommand."""

from __future__ import annotations

from typing import Callable

from cli.subcommands._shared import add_json_flag, add_shared_flags, group_dispatch


def build_snapshot_parser(subparsers, *, cmd_snapshot: Callable) -> None:
    """Attach the ``snapshot`` subcommand (including show / save / clear / path) to ``subparsers``."""
    parser = subparsers.add_parser(
        "snapshot", help="Snapshot (show / save / clear / path)",
        description="View / write / clear the snapshot (model config + config + workspace).")
    sub = parser.add_subparsers(dest="snapshot_action")

    show = sub.add_parser("show", help="Show the current snapshot")
    add_shared_flags(show, inherited=True)
    add_json_flag(show, "Output the full snapshot as JSON")
    show.set_defaults(func=cmd_snapshot)

    save = sub.add_parser("save", help="Write the snapshot immediately")
    add_shared_flags(save, inherited=True)
    save.set_defaults(func=cmd_snapshot)

    clear = sub.add_parser("clear", help="Delete the snapshot file")
    add_shared_flags(clear, inherited=True)
    clear.set_defaults(func=cmd_snapshot)

    path = sub.add_parser("path", help="Print the snapshot file path")
    add_shared_flags(path, inherited=True)
    path.set_defaults(func=cmd_snapshot)

    parser.set_defaults(func=group_dispatch(parser, cmd_snapshot, action_dest="snapshot_action"))
