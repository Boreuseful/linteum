"""Shared argparse helper functions for ``cli/subcommands/*``.

This module **does not import ``main``** (to avoid circular imports); ``main`` and every parser
builder take the shared logic from here, keeping the top level and subcommands consistent for the
same set of flags.
"""

from __future__ import annotations

import argparse


def add_shared_flags(parser: argparse.ArgumentParser, *, inherited: bool = False) -> None:
    """Add flags shared by the top level / subcommands: ``-w/--workspace``, ``-s/--session``, ``--config``, ``-y``.

    When ``inherited=True`` (the subcommand side), the default uses ``argparse.SUPPRESS``: the
    attribute is only written to the namespace when **explicitly given** on the command line, so the
    ``X`` parsed at the top level by ``linteum --workspace X chat`` is not overwritten by the chat
    sub-parser's default (the top level and subcommand share the same namespace).
    """
    suppress = argparse.SUPPRESS
    default = suppress if inherited else None

    parser.add_argument(
        "-w", "--workspace", metavar="PATH", default=default,
        help="Workspace directory (defaults to the directory the command was started in)")
    parser.add_argument(
        "-s", "--session", metavar="ID", default=default,
        help="Session ID (searched automatically by workspace by default)")
    parser.add_argument(
        "--config", metavar="PATH", default=default,
        help="Config file path (default ~/.agent/config.yaml)")

    if inherited:
        parser.add_argument(
            "-y", "--auto-approve", dest="auto_approve", action="store_true",
            default=suppress,
            help="Auto-approve tool calls that require authorization (such as bash)")
    else:
        parser.add_argument(
            "-y", "--auto-approve", dest="auto_approve", action="store_true",
            default=False,
            help="Auto-approve tool calls that require authorization (such as bash)")

    # Snapshot: only chat writes it; --use-snapshot only reads the workspace from the snapshot.
    parser.add_argument(
        "--snapshot", dest="snapshot", action="store_true",
        default=(suppress if inherited else True),
        help="After chat runs, write the model config / config / workspace to the snapshot (enabled by default; only chat writes)")
    parser.add_argument(
        "--no-snapshot", dest="snapshot", action="store_false", default=suppress,
        help="Do not write the snapshot for this chat run")
    parser.add_argument(
        "--use-snapshot", dest="use_snapshot", action="store_true",
        default=(suppress if inherited else False),
        help="Use the workspace saved in the snapshot (defaults to the command's start directory)")


def add_json_flag(parser: argparse.ArgumentParser, help: str) -> None:
    """Add ``--json`` (store_true) for scripts to consume command output."""
    parser.add_argument("--json", action="store_true", help=help)


def group_dispatch(parser: argparse.ArgumentParser, handler, *, action_dest: str):
    """Build the dispatch function for a group command with sub-actions (for ``set_defaults(func=...)``).

    - No sub-action given (e.g. ``linteum sessions``) -> print that group's ``--help`` and return 0,
      rather than exiting silently;
    - Sub-action given -> hand it to the injected ``handler``.

    Same pattern as hermes' ``_dispatch_sessions``: the parser is injected via a closure while the
    handler is still the one dependency-injected by ``main``, so this module still does not import
    ``main``.
    """

    def _dispatch(args):
        if getattr(args, action_dest, None) is None:
            parser.print_help()
            return 0
        return handler(args)

    return _dispatch
