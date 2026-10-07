"""The ``linteum`` command-line entry point: register subcommands, parse ``argv``, dispatch to handlers.

Architecture (mirroring hermes' Builder + dependency injection):

- ``cli/subcommands/<group>.py`` **only** provides
  ``build_<group>_parser(subparsers, *, cmd_<group>=...)``; the handler is injected by this module,
  and those modules **never import main** (to avoid circular imports).
- ``_forward_command()`` is a **lazy import factory**: the handler only calls
  ``importlib.import_module(target)`` when invoked, so
  (a) the fast path pays no startup cost for heavy modules; (b) ``patch("<module>.<attr>")`` can still
  intercept.
- ``_build_cli_parser()`` calls ``build_<group>_parser(subparsers, cmd_<group>=handler)`` one by one;
  **the registration order is the ``linteum --help`` order**.
- Unified dispatch contract: every leaf parser uses ``set_defaults(func=handler)`` and ``main()``
  uniformly calls ``args.func(args)``; when a handler returns an ``int`` it is used as the process
  exit code (``None`` = success).

Both ``python -m cli.main`` and ``python cli/main.py`` are supported.
"""

from __future__ import annotations

import os
import sys

# Allow running `python cli/main.py` directly: ensure the project root is on sys.path so the
# intra-package absolute imports `cli.*` and `linteum_agent.*` resolve (no side effect under
# `python -m cli.main`).
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from cli import PROG, __version__, snapshot  # noqa: E402 - depends on the sys.path handling above
from cli._parser import build_top_level_parser  # noqa: E402
from cli.subcommands.chat import build_chat_parser  # noqa: E402
from cli.subcommands.config import build_config_parser  # noqa: E402
from cli.subcommands.model import build_model_parser  # noqa: E402
from cli.subcommands.sessions import build_sessions_parser  # noqa: E402
from cli.subcommands.setup import build_setup_parser  # noqa: E402
from cli.subcommands.skills import build_skills_parser  # noqa: E402
from cli.subcommands.snapshot import build_snapshot_parser  # noqa: E402
from cli.subcommands.status import build_status_parser  # noqa: E402
from cli.subcommands.tools import build_tools_parser  # noqa: E402

# ----------------------------------------------------------------------
# Lazy import factory
# ----------------------------------------------------------------------


def _forward_command(name: str, module: str, attr: str, *, forward_return: bool = False,
                     doc: str = ""):
    """Return a ``linteum <cmd>`` handler: only hands ``args`` to ``<module>.<attr>`` when invoked.

    ``importlib.import_module`` is deferred to **invocation time**, so
    (a) startup / ``--help`` pays no import cost for the implementation module;
    (b) ``patch("<module>.<attr>")`` can still intercept the real implementation.
    With ``forward_return=True`` the implementation's return code is propagated to ``main()`` as the
    process exit code.
    """

    def _cmd(args):
        import importlib

        result = getattr(importlib.import_module(module), attr)(args)
        return result if forward_return else None

    _cmd.__name__ = _cmd.__qualname__ = name
    _cmd.__doc__ = doc or None
    return _cmd


# Handlers are defined only here by main and injected into subcommand builders; the implementations live in cli.commands.* (lazy import).
cmd_chat = _forward_command(
    "cmd_chat", "cli.commands.chat", "run_chat", forward_return=True, doc="Chat with the Agent.")
cmd_setup = _forward_command(
    "cmd_setup", "cli.commands.setup", "run_setup", forward_return=True, doc="Initialize config and project state.")
cmd_snapshot = _forward_command(
    "cmd_snapshot", "cli.commands.snapshot", "run_snapshot", forward_return=True, doc="Snapshot management.")
cmd_sessions = _forward_command(
    "cmd_sessions", "cli.commands.sessions", "run_sessions", forward_return=True, doc="Session history.")
cmd_config = _forward_command(
    "cmd_config", "cli.commands.config", "run_config", forward_return=True, doc="Config read/write.")
cmd_model = _forward_command(
    "cmd_model", "cli.commands.model", "run_model", forward_return=True, doc="Model selection.")
cmd_tools = _forward_command(
    "cmd_tools", "cli.commands.tools", "run_tools", forward_return=True, doc="Tool list.")
cmd_skills = _forward_command(
    "cmd_skills", "cli.commands.skills", "run_skills", forward_return=True, doc="Skill list.")
cmd_status = _forward_command(
    "cmd_status", "cli.commands.status", "run_status", forward_return=True, doc="Runtime status.")


# ----------------------------------------------------------------------
# Subcommand list / default command
# ----------------------------------------------------------------------

#: All built-in subcommand names (strictly consistent with the registration order / set of ``_build_cli_parser``).
_BUILTIN_SUBCOMMANDS = frozenset(
    {"chat", "setup", "snapshot", "sessions", "config", "model", "tools", "skills", "status"})

#: Without a subcommand it falls to ``chat``; these are the defaults for chat-only fields.
_CHAT_DEFAULTS = {
    "prompt": None,
    "stream": True,
    "new": False,
    "enable_bash": False,
    "max_tool_rounds": 20,
    "json": False,
}


def _default_to_chat(args):
    """``linteum`` (no subcommand) -> go through ``cmd_chat``, filling in unparsed chat defaults."""
    args.command = "chat"
    for name, default in _CHAT_DEFAULTS.items():
        if not hasattr(args, name):
            setattr(args, name, default)
    return cmd_chat(args)


# ----------------------------------------------------------------------
# Parser tree
# ----------------------------------------------------------------------


def _build_cli_parser():
    """Build the full ``linteum`` argparse tree -> ``(parser, subparsers)``.

    The registration order is the ``linteum --help`` order and is kept stable. Each group's parser is
    built in ``cli/subcommands/<group>.py``, and the handler is injected here, so those modules never
    import main.
    """
    parser, subparsers = build_top_level_parser()

    build_chat_parser(subparsers, cmd_chat=cmd_chat)
    build_setup_parser(subparsers, cmd_setup=cmd_setup)
    build_snapshot_parser(subparsers, cmd_snapshot=cmd_snapshot)
    build_sessions_parser(subparsers, cmd_sessions=cmd_sessions)
    build_config_parser(subparsers, cmd_config=cmd_config)
    build_model_parser(subparsers, cmd_model=cmd_model)
    build_tools_parser(subparsers, cmd_tools=cmd_tools)
    build_skills_parser(subparsers, cmd_skills=cmd_skills)
    build_status_parser(subparsers, cmd_status=cmd_status)

    return parser, subparsers


def _parse_cli_args(parser, subparsers, argv):
    """Parse ``argv``; the subcommand may be omitted (bare ``linteum`` falls to chat), so subparsers is not required."""
    subparsers.required = False
    return parser.parse_args(argv)


def _cmd_version() -> None:
    print(f"{PROG} {__version__}")


def _run(func, args) -> int:
    """Execute the handler: ``BrokenPipeError`` (e.g. output closed early by ``head``) exits silently."""
    try:
        result = func(args)
    except BrokenPipeError:
        # Point stdout at devnull to avoid another flush stack trace when the interpreter exits.
        try:
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, sys.stdout.fileno())
        except OSError:  # pragma: no cover - fallback
            pass
        return 0
    return result if isinstance(result, int) else 0


def main(argv=None) -> int:
    """CLI main entry point: parse -> handle global flags -> dispatch to ``args.func``; return the process exit code."""
    argv = list(sys.argv[1:] if argv is None else argv)

    parser, subparsers = _build_cli_parser()
    args = _parse_cli_args(parser, subparsers, argv)

    if getattr(args, "version", False):
        _cmd_version()
        return 0

    # Snapshot (enabled by default): resolve the workspace (default: the command's start directory /
    # --use-snapshot) + write the snapshot.
    # The setup / snapshot subcommands manage this themselves; boot() skips them.
    snapshot.boot(args)

    # No subcommand (top-level -w / -s / -y allowed) -> default to chat; bare `linteum` enters interactive conversation.
    if args.command is None:
        return _run(_default_to_chat, args)

    func = getattr(args, "func", None)
    if func is None:
        parser.print_help()
        return 0

    return _run(func, args)


if __name__ == "__main__":
    sys.exit(main())

