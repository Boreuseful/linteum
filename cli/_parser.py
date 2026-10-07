"""Top-level argparse construction.

This module is only responsible for the **top-level parser** (``prog`` / top-level flags / the
subparsers action) and a custom ``ArgumentParser``; every subcommand parser except ``chat`` (the
default command, built by ``subcommands/chat.py``) is provided by ``cli/subcommands/<group>.py`` and
``main._build_cli_parser`` calls ``build_<group>_parser(subparsers, ...)`` one by one and injects the
handler.
"""

from __future__ import annotations

import argparse
import difflib

from cli import PROG
from cli.subcommands._shared import add_shared_flags

#: The usage examples at the end of ``linteum --help`` (together with the registration order, they form the user-visible command surface).
_EPILOGUE = """\
Examples:
  linteum                                 enter interactive conversation (writes snapshot by default)
  linteum setup                           initialize ~/.agent/config.yaml
  linteum chat -p "summarize this repo"   single-shot Q&A
  linteum chat --bash -y                  enable the bash tool and auto-approve
  linteum --use-snapshot                  start with the workspace saved in the snapshot
  linteum snapshot show                   view the snapshot (model config + config + workspace)
  linteum sessions list                   list sessions in the workspace
  linteum status                          view runtime status
"""


class CliArgumentParser(argparse.ArgumentParser):
    """Give a three-line error + closest suggestion for an unknown subcommand instead of printing the whole command tree.

    Native argparse, when the first positional is not a registered subcommand, prints the full usage +
    a very long ``(choose from '...', ...)``, drowning out the only useful information (this word is
    not a command). Here it is changed to three lines + ``difflib``'s "Did you mean"; other errors keep
    argparse's native form.
    """

    def _check_value(self, action, value):  # noqa: D401 - overrides an argparse hook
        if isinstance(action, argparse._SubParsersAction) and value not in action.choices:
            lines = [f"{self.prog}: '{value}' is not a `{self.prog}` command."]
            close = difflib.get_close_matches(str(value), list(action.choices), n=3, cutoff=0.6)
            if close:
                lines.append(f"Did you mean: {', '.join(close)}?")
            lines.append(f"Run `{self.prog} --help` to see all commands.")
            self.exit(2, "\n".join(lines) + "\n")
        super()._check_value(action, value)


def build_top_level_parser() -> tuple[argparse.ArgumentParser, argparse._SubParsersAction]:
    """Build the top-level parser and its ``subparsers`` action.

    Returns ``(parser, subparsers)``; the caller is responsible for registering subcommands via
    ``subparsers.add_parser(...)`` in order and calling ``set_defaults(func=...)`` on leaf parsers.
    """
    parser = CliArgumentParser(
        prog=PROG,
        description="linteum_agent -- a command-line Agent with tool-calling capability",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=_EPILOGUE,
    )
    _add_top_level_flags(parser)
    # metavar=<command> keeps the usage line as ``linteum [...] <command>`` instead of spreading out
    # all subcommand names; ``linteum --help`` still lists every command individually.
    subparsers = parser.add_subparsers(dest="command", help="the subcommand to run", metavar="<command>")
    return parser, subparsers


def _add_top_level_flags(parser: argparse.ArgumentParser) -> None:
    """Top-level flags: ``--version`` + the workspace/session/config/approval flags shared with subcommands."""
    parser.add_argument(
        "--version", action="store_true", help="print the version and exit")
    # Workspace / session / config / auto-approval: appear at the top level and are also inherited by subcommands (see _shared.add_shared_flags).
    add_shared_flags(parser, inherited=False)
