"""Parser for the ``linteum sessions`` subcommand."""

from __future__ import annotations

from typing import Callable

from cli.subcommands._shared import add_json_flag, add_shared_flags, group_dispatch


def build_sessions_parser(subparsers, *, cmd_sessions: Callable) -> None:
    """Attach the ``sessions`` subcommand (list / show / new / switch / current) to ``subparsers``."""
    parser = subparsers.add_parser(
        "sessions", help="Session history (list / show / new / switch / current)",
        description="View, create, and switch sessions in .agent/sessions under the workspace and user home directory.")
    sub = parser.add_subparsers(dest="sessions_action")

    lst = sub.add_parser("list", help="List session summaries")
    add_shared_flags(lst, inherited=True)
    lst.add_argument(
        "--workspace-only", dest="workspace_only", action="store_true",
        help="List only sessions in the workspace directory, excluding the user-level ~/.agent/sessions")
    lst.add_argument(
        "--current-workspace", dest="current_workspace", action="store_true",
        help="List only sessions matching the current workspace")
    add_json_flag(lst, "Output session summaries as JSON")
    lst.set_defaults(func=cmd_sessions)

    show = sub.add_parser("show", help="View a session's messages and usage")
    add_shared_flags(show, inherited=True)
    show.add_argument("session_id", help="Session ID (a unique prefix suffices)")
    add_json_flag(show, "Output as JSON")
    show.set_defaults(func=cmd_sessions)

    new = sub.add_parser("new", help="Create a new session (and set it as the current session)")
    add_shared_flags(new, inherited=True)
    new.add_argument(
        "--id", dest="session_id", default=None, metavar="ID",
        help="Specify the session ID (generates a uuid4 by default)")
    new.add_argument(
        "--no-switch", dest="switch", action="store_false", default=True,
        help="Only create; do not set it as the current session")
    new.add_argument(
        "--force", action="store_true", default=False,
        help="Overwrite the existing same-named session with a brand-new one")
    add_json_flag(new, "Output as JSON")
    new.set_defaults(func=cmd_sessions)

    switch = sub.add_parser("switch", help="Switch the current session (the session must already exist)")
    add_shared_flags(switch, inherited=True)
    switch.add_argument("session_id", help="Session ID (a unique prefix suffices)")
    switch.set_defaults(func=cmd_sessions)

    current = sub.add_parser("current", help="Show the current session")
    add_shared_flags(current, inherited=True)
    add_json_flag(current, "Output as JSON")
    current.set_defaults(func=cmd_sessions)

    parser.set_defaults(func=group_dispatch(parser, cmd_sessions, action_dest="sessions_action"))
