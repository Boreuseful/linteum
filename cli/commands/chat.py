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


def _same_workspace(recorded, workspace) -> bool:
    """Whether two workspace paths denote the same directory (resolved; tolerant of symlinks)."""
    try:
        return Path(recorded).expanduser().resolve() == Path(workspace).expanduser().resolve()
    except OSError:  # pragma: no cover - fallback (e.g. a broken symlink)
        return Path(recorded).expanduser() == Path(workspace).expanduser()


def _session_in_workspace(session_id: str, workspace: Path) -> bool:
    """Whether ``session_id`` already has a copy that belongs to ``workspace`` (read-only, never creates)."""
    from linteum_agent.run_agent import find_context_json

    return find_context_json(workspace, session_id=session_id) is not None


def _latest_session_id(workspace: Path) -> str | None:
    """The most recently updated session of ``workspace`` (``None`` when the workspace has none)."""
    from linteum_agent.run_agent import list_sessions

    infos = list_sessions(workspace, only_current_workspace=True)
    return infos[0].session_id if infos else None


def _session_for_workspace(workspace: Path) -> str | None:
    """Pick the session ``chat`` should use in ``workspace`` and keep the current-session pointer scoped.

    The current session is stored per workspace, so switching to a different workspace must not keep
    reusing the previous workspace's session:

    - the pointer already belongs to ``workspace`` -> reuse it (**fast path**, no disk scan);
    - a legacy pointer without a workspace, or one from another workspace -> reuse it only when that
      session really lives in ``workspace``; otherwise fall through;
    - the newest session of ``workspace`` -> switch to it (and record it as the current session);
    - ``workspace`` has no session -> return ``None`` so ``AgentRunner`` mints a brand-new one (the
      fresh session is then recorded by ``_prepare_session``).
    """
    from cli.session_state import get_current_session_entry, set_current_session

    entry = get_current_session_entry()
    if entry is not None:
        recorded_workspace = entry.workspace
        if recorded_workspace is not None and _same_workspace(recorded_workspace, workspace):
            return entry.session_id  # fast path: the pointer is already scoped to this workspace
        if _session_in_workspace(entry.session_id, workspace):
            set_current_session(entry.session_id, workspace=workspace)
            return entry.session_id

    latest = _latest_session_id(workspace)
    if latest is not None:
        set_current_session(latest, workspace=workspace)
        return latest
    return None


# Friendly notice when tool rounds are exceeded: give one actionable sentence instead of exposing the
# underlying RuntimeError text directly.
MAX_TOOL_ROUNDS_NOTICE = "(maximum tool-call rounds reached; this turn has stopped. Use --max-tool-rounds to raise the limit)"


def _warn_max_tool_rounds() -> None:
    """Print the friendly "tool rounds exceeded" notice (body goes to stdout, the notice to stderr)."""
    print(MAX_TOOL_ROUNDS_NOTICE, file=sys.stderr)


def _build_runner(args, workspace: Path | None = None):
    """Build an ``AgentRunner`` from argv (lazy import to avoid startup cost for other subcommands).

    Session selection: explicit ``-s/--session`` > the current session **of this workspace** (set by
    ``linteum sessions new/switch`` or by a previous ``chat``) > the workspace's newest session > a
    brand-new session created by ``AgentRunner``. The current session is scoped to the workspace, so
    switching workspaces never reuses another workspace's session id.
    """
    from linteum_agent.run_agent import build_agent

    if workspace is None:
        workspace = _workspace(args)
    explicit = getattr(args, "session", None)
    session_id = explicit or _session_for_workspace(workspace)
    return build_agent(
        workspace=workspace,
        session_id=session_id,
        config_path=getattr(args, "config", None),
        auto_approve=bool(getattr(args, "auto_approve", False)),
        enable_bash=bool(getattr(args, "enable_bash", False)),
        max_tool_rounds=int(getattr(args, "max_tool_rounds", 20) or 20),
    )


def _prepare_session(runner, args) -> None:
    """Decide between "create new" and "continue", and keep the current-session pointer workspace-scoped.

    - ``--new``: create a brand-new session and make it the current session;
    - an existing workspace session was found: load (continue) it;
    - a brand-new workspace session was minted by ``AgentRunner`` (no ``-s``): record it as the
      current session, together with the workspace, so the next run here reuses it instead of
      another workspace's session (and so the fast workspace check hits).
    """
    from cli.session_state import set_current_session

    workspace = _workspace(args)
    if getattr(args, "new", False):
        runner.create_context()
        set_current_session(runner.session_id, workspace=workspace)
    elif getattr(runner, "has_unloaded_session", False):
        runner.load_session()
    elif not getattr(args, "session", None):
        set_current_session(runner.session_id, workspace=workspace)


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
