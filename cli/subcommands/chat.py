"""Parser for the ``linteum chat`` subcommand (also the default command when no subcommand is given)."""

from __future__ import annotations

from typing import Callable

from cli.subcommands._shared import add_shared_flags


def build_chat_parser(subparsers, *, cmd_chat: Callable) -> None:
    """Attach the ``chat`` subcommand to ``subparsers``."""
    parser = subparsers.add_parser(
        "chat",
        help="Chat with the Agent (interactive REPL or single-shot Q&A)",
        description="Start an Agent session: omit --prompt to enter the interactive REPL.",
    )
    # Workspace / session / config / auto-approval: same names as top level, using SUPPRESS defaults to preserve the top-level parse result.
    add_shared_flags(parser, inherited=True)
    parser.add_argument(
        "-p", "--prompt", metavar="TEXT", default=None,
        help="Prompt for a single-shot Q&A; omit to enter the interactive REPL")
    parser.add_argument(
        "--stream", dest="stream", action="store_true", default=True,
        help="Streaming output (enabled by default)")
    parser.add_argument(
        "--no-stream", dest="stream", action="store_false",
        help="Disable streaming output and wait for the whole reply")
    parser.add_argument(
        "--new", action="store_true", default=False,
        help="Force a new session (ignore existing context in the workspace)")
    parser.add_argument(
        "--bash", dest="enable_bash", action="store_true", default=False,
        help="Enable the bash tool (requires approval before execution unless --auto-approve)")
    parser.add_argument(
        "--max-tool-rounds", dest="max_tool_rounds", type=int, default=20, metavar="N",
        help="Maximum number of tool-call rounds within a single request (default 20)")
    parser.add_argument(
        "--json", action="store_true", default=False,
        help="Output the single-shot Q&A result as JSON (for script consumption)")
    parser.set_defaults(func=cmd_chat)
