"""Implementation of ``linteum sessions``: list / show / create / switch sessions (no Agent construction)."""

from __future__ import annotations

import json
import sys
from pathlib import Path


def _workspace(args) -> str:
    workspace = getattr(args, "workspace", None)
    return str(Path(workspace).expanduser()) if workspace else str(Path.cwd())


def run_sessions(args) -> int:
    action = getattr(args, "sessions_action", None)
    if action == "list":
        return _list(args)
    if action == "show":
        return _show(args)
    if action == "new":
        return _new(args)
    if action == "switch":
        return _switch(args)
    if action == "current":
        return _current(args)
    print("Usage: linteum sessions {list,show,new,switch,current} ...", file=sys.stderr)
    return 2


def _list(args) -> int:
    from linteum_agent.run_agent import list_sessions

    infos = list_sessions(
        _workspace(args),
        include_project=not bool(getattr(args, "workspace_only", False)),
        only_current_workspace=bool(getattr(args, "current_workspace", False)),
    )
    if getattr(args, "json", False):
        print(json.dumps([info.to_dict() for info in infos], ensure_ascii=False, indent=2,
                         default=str))
        return 0
    if not infos:
        print("(no sessions found)")
        return 0
    for info in infos:
        workspace = str(info.workspace) if info.workspace is not None else "-"
        print(
            f"{info.session_id}  [{info.scope}]  "
            f"msgs={info.message_count} rounds={info.round_count}  "
            f"updated={info.updated_at}  workspace={workspace}"
        )
    return 0


def _resolve_session(workspace: str, needle: str):
    """Resolve a session by ID or unique prefix; return a ``SessionInfo`` / an ambiguous-prefix list / ``None``."""
    from linteum_agent.run_agent import list_sessions

    infos = list_sessions(workspace)
    for info in infos:
        if info.session_id == needle:
            return info
    prefix = [info for info in infos if info.session_id.startswith(needle)]
    if len(prefix) == 1:
        return prefix[0]
    if len(prefix) > 1:
        return prefix
    return None


def _show(args) -> int:
    from linteum_agent.context import ContextStore

    workspace = _workspace(args)
    needle = args.session_id
    found = _resolve_session(workspace, needle)
    if found is None:
        print(f"Session not found: {needle}", file=sys.stderr)
        return 1
    if isinstance(found, list):
        print(f"Prefix '{needle}' matched multiple sessions:", file=sys.stderr)
        for info in found:
            print(f"  {info.session_id}", file=sys.stderr)
        return 2

    store = ContextStore(workspace=workspace)
    store.load(found.path, merge=True)
    session = store.get_session(found.session_id)
    if session is None:
        print(f"Unable to read session: {found.session_id}", file=sys.stderr)
        return 1

    messages = session.messages()
    if getattr(args, "json", False):
        print(json.dumps(
            {
                "session_id": found.session_id,
                "path": str(found.path),
                "workspace": str(found.workspace) if found.workspace is not None else None,
                "usage": dict(session.usage),
                "messages": [_message_dict(message) for message in messages],
            },
            ensure_ascii=False, indent=2, default=str,
        ))
        return 0

    print(f"session  : {found.session_id}")
    print(f"file     : {found.path}")
    print(f"workspace: {found.workspace if found.workspace is not None else '(inherits current workspace)'}")
    print(f"usage    : {dict(session.usage)}")
    print(f"messages : {len(messages)}")
    print("-" * 60)
    for message in messages:
        _print_message(message)
    return 0


def _new(args) -> int:
    """Create a new session (writes two copies to .agent/sessions by default) and set it as the current session."""
    from linteum_agent.run_agent import create_context_json, new_session_id

    session_id = getattr(args, "session_id", None) or new_session_id()
    paths = create_context_json(
        session_id, _workspace(args), overwrite=bool(getattr(args, "force", False)))

    switched = bool(getattr(args, "switch", True))
    if switched:
        from cli.session_state import set_current_session

        set_current_session(session_id, workspace=_workspace(args))

    if getattr(args, "json", False):
        print(json.dumps(
            {"session_id": session_id, "paths": [str(p) for p in paths], "switched": switched},
            ensure_ascii=False, indent=2, default=str))
        return 0
    print(f"new session : {session_id}")
    for path in paths:
        print(f"file        : {path}")
    print(f"current     : {'switched' if switched else 'not switched (--no-switch)'}")
    return 0


def _switch(args) -> int:
    """Set an existing session as the current session (a unique prefix is supported)."""
    from cli.session_state import set_current_session

    workspace = _workspace(args)
    needle = args.session_id
    found = _resolve_session(workspace, needle)
    if found is None:
        print(f"Session not found: {needle} (create it first with `linteum sessions new --id {needle}`)",
              file=sys.stderr)
        return 1
    if isinstance(found, list):
        print(f"Prefix '{needle}' matched multiple sessions:", file=sys.stderr)
        for info in found:
            print(f"  {info.session_id}", file=sys.stderr)
        return 2

    set_current_session(found.session_id, workspace=workspace)
    print(f"current     : {found.session_id}")
    print(f"file        : {found.path}")
    print(f"workspace   : {workspace}")
    return 0


def _current(args) -> int:
    """Show the current session (give a hint when unset; not an error)."""
    from cli.session_state import current_session_path, get_current_session_entry

    entry = get_current_session_entry()
    if entry is None:
        if getattr(args, "json", False):
            print(json.dumps({"session_id": None}, ensure_ascii=False))
        else:
            print("(no current session set; create one with `linteum sessions new`, "
                  "or switch with `linteum sessions switch <id>`)")
        return 0

    session_id = entry.session_id
    found = _resolve_session(_workspace(args), session_id)
    path = None if (found is None or isinstance(found, list)) else str(found.path)
    workspace = str(entry.workspace) if entry.workspace is not None else None
    if getattr(args, "json", False):
        print(json.dumps(
            {"session_id": session_id, "path": path, "workspace": workspace,
             "current_session_path": str(current_session_path())},
            ensure_ascii=False, indent=2))
        return 0
    print(f"current     : {session_id}")
    if workspace is not None:
        print(f"workspace   : {workspace}")
    if path is not None:
        print(f"file        : {path}")
    return 0


def _message_dict(message) -> dict:
    return {
        "role": getattr(message, "role", None),
        "content": getattr(message, "content", None),
        "tool_calls": [getattr(call, "name", None) for call in getattr(message, "tool_calls", []) or []],
    }


def _print_message(message) -> None:
    role = getattr(message, "role", None) or "?"
    print(f"[{role}]")
    content = getattr(message, "content", None)
    if isinstance(content, str):
        if content:
            print(content)
    elif content is not None:
        print(json.dumps(content, ensure_ascii=False, indent=2, default=str))
    for call in getattr(message, "tool_calls", []) or []:
        print(f"  -> tool_call {getattr(call, 'name', call)}")
