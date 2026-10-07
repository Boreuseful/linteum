"""Implementation of ``linteum chat``: build an :class:`AgentRunner` and converse.

This module only does **host adaptation** (read argv -> call linteum_agent) and contains no Agent
business logic; the real capability comes from ``linteum_agent.run_agent.build_agent``.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any


def _workspace(args) -> Path:
    workspace = getattr(args, "workspace", None)
    return Path(workspace).expanduser() if workspace else Path.cwd()


# Friendly notice when tool rounds are exceeded: give one actionable sentence instead of exposing the
# underlying RuntimeError text directly.
MAX_TOOL_ROUNDS_NOTICE = "(maximum tool-call rounds reached; this turn has stopped. Use --max-tool-rounds to raise the limit)"


def _warn_max_tool_rounds() -> None:
    """Print the friendly "tool rounds exceeded" notice (body goes to stdout, the notice to stderr)."""
    print(MAX_TOOL_ROUNDS_NOTICE, file=sys.stderr)


def _build_runner(args):
    """Build an ``AgentRunner`` from argv (lazy import to avoid startup cost for other subcommands).

    Session selection: explicit ``-s/--session`` > the **current session** (set by
    ``linteum sessions new/switch``) > automatic lookup / creation by ``AgentRunner`` based on the
    workspace.
    """
    from linteum_agent.run_agent import build_agent
    from cli.session_state import get_current_session

    session_id = getattr(args, "session", None) or get_current_session()
    return build_agent(
        workspace=_workspace(args),
        session_id=session_id,
        config_path=getattr(args, "config", None),
        auto_approve=bool(getattr(args, "auto_approve", False)),
        enable_bash=bool(getattr(args, "enable_bash", False)),
        max_tool_rounds=int(getattr(args, "max_tool_rounds", 20) or 20),
    )


def _prepare_session(runner, args) -> None:
    """Decide between "create new" and "continue": ``--new`` creates and sets the current session, otherwise load the found existing session."""
    if getattr(args, "new", False):
        from cli.session_state import set_current_session

        runner.create_context()
        set_current_session(runner.session_id)
    elif getattr(runner, "has_unloaded_session", False):
        runner.load_session()


class _ConsolePrinter:
    """Render the event stream into terminal output (text / reasoning / tool calls)."""

    def __init__(self) -> None:
        self._pending_text = False

    def on_event(self, event: dict[str, Any]) -> None:
        event_type = event.get("type")
        if event_type == "message_update":
            update = event.get("assistant_message_event") or {}
            kind = update.get("type")
            if kind == "text_delta":
                self._write(update.get("text", ""))
            elif kind == "reasoning_delta":
                self._write(f"\033[90m{update.get('text', '')}\033[0m")
        elif event_type == "tool_execution_start":
            self.ensure_newline()
            print(f"[tool] {event.get('tool_name')} {event.get('args')}")
        elif event_type == "tool_execution_end":
            status = "error" if event.get("is_error") else "ok"
            print(f"[tool] {event.get('tool_name')} -> {status}")

    def _write(self, text: str) -> None:
        if not text:
            return
        sys.stdout.write(text)
        sys.stdout.flush()
        self._pending_text = True

    def ensure_newline(self) -> None:
        if self._pending_text:
            sys.stdout.write("\n")
            sys.stdout.flush()
            self._pending_text = False


def run_chat(args) -> int:
    try:
        runner = _build_runner(args)
    except Exception as exc:  # missing config / API key not set, etc.
        print(f"Unable to initialize Agent: {exc}", file=sys.stderr)
        return 2

    try:
        _prepare_session(runner, args)
        prompt = getattr(args, "prompt", None)
        if prompt:
            return _one_shot(runner, args, prompt)
        return _repl(runner, args)
    finally:
        runner.close()


def _one_shot(runner, args, prompt: str) -> int:
    from linteum_agent.agent import MaxToolRoundsError

    stream = bool(getattr(args, "stream", True))
    as_json = bool(getattr(args, "json", False))

    if stream and not as_json:
        printer = _ConsolePrinter()
        detach = runner.add_listener(printer)
        try:
            for _chunk in runner.stream(prompt):
                pass
        except MaxToolRoundsError:
            # Do not raise or print the exception; the body was already streamed by the printer, so just
            # add a friendly notice.
            printer.ensure_newline()
            _warn_max_tool_rounds()
            return 1
        finally:
            detach()
        printer.ensure_newline()
        return 0 if runner.response().ok else 1

    try:
        response = runner.send(prompt)
    except MaxToolRoundsError:
        # Fall back to the aggregated response (the text may be empty) instead of printing the RuntimeError to the terminal.
        response = runner.response()
        if not as_json:
            if response.reasoning:
                print(f"\033[90m{response.reasoning}\033[0m")
            print(response.text)
        _warn_max_tool_rounds()
        return 1
    if as_json:
        print(response.to_json())
    else:
        if response.reasoning:
            print(f"\033[90m{response.reasoning}\033[0m")
        print(response.text)
    return 0 if response.ok else 1


def _repl(runner, args) -> int:
    from linteum_agent.agent import MaxToolRoundsError

    print(f"linteum session {runner.session_id} (type exit / quit to quit)")
    printer = _ConsolePrinter()
    detach = runner.add_listener(printer)
    try:
        while True:
            try:
                line = input("> ")
            except EOFError:
                print()
                break
            text = line.strip()
            if not text:
                continue
            if text in ("exit", "quit", ":q"):
                break
            try:
                for _chunk in runner.stream(text):
                    pass
            except MaxToolRoundsError:
                # Tool rounds exceeded within a single turn: do not print the exception; show a friendly notice and continue the REPL.
                printer.ensure_newline()
                _warn_max_tool_rounds()
            except Exception as exc:  # a single-turn failure does not exit the REPL
                print(f"Request failed: {exc}", file=sys.stderr)
            printer.ensure_newline()
    finally:
        detach()
    return 0
