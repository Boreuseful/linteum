"""The current-session pointer.

`linteum sessions new/switch` writes the selected session ID into ``~/.agent/current_session``;
`linteum chat` **reuses** that session by default when ``-s/--session`` is not explicitly given.

- Default location: ``~/.agent/current_session`` (a small text record: the session ID on the first
  line, plus an optional ``workspace=<path>`` line recording which workspace it belongs to);
- The recorded workspace lets ``chat`` tell **in O(1)** whether the current session belongs to the
  workspace it starts in: if not, it switches to that workspace's newest session (or creates a
  brand-new one when the workspace has none), so sessions never leak across workspaces;
- The environment variable ``linteum_agent_CURRENT_SESSION`` can override the file path (tests /
  multiple projects);
- Both reading and writing tolerate a missing file: reading returns ``None`` (meaning "no current
  session set") and never raises; a legacy file that only contains a session ID is still understood
  (its workspace is then ``None`` = unknown).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, NamedTuple

#: Current session file name (located in the user-level ``~/.agent``).
CURRENT_SESSION_FILENAME = "current_session"
#: Environment variable: overrides the current session file path.
CURRENT_SESSION_PATH_ENV = "linteum_agent_CURRENT_SESSION"
#: Prefix of the line that records the workspace path inside the current-session file.
WORKSPACE_LINE_PREFIX = "workspace="


class CurrentSession(NamedTuple):
    """The current-session record: the session ID plus the workspace it was last used in.

    ``workspace`` is ``None`` for a legacy pointer (written before the workspace was recorded):
    the caller then cannot rely on the fast path and must verify the session's workspace.
    """

    session_id: str
    workspace: Path | None = None

    def to_dict(self) -> dict[str, Any]:
        """A directly JSON-serializable view (``workspace`` becomes a string)."""
        return {
            "session_id": self.session_id,
            "workspace": str(self.workspace) if self.workspace is not None else None,
        }


def current_session_path() -> Path:
    """The current session file path (the ``linteum_agent_CURRENT_SESSION`` env var can override it)."""
    override = os.getenv(CURRENT_SESSION_PATH_ENV)
    if override and override.strip():
        return Path(override).expanduser()
    from cli.snapshot import agent_dir

    return agent_dir() / CURRENT_SESSION_FILENAME


def get_current_session_entry() -> CurrentSession | None:
    """Read the full current-session record (session ID + workspace); ``None`` when unset / unreadable.

    Tolerates a legacy file that only contains a session ID (its ``workspace`` is then ``None``).
    """
    try:
        text = current_session_path().read_text(encoding="utf-8")
    except OSError:
        return None
    session_id: str | None = None
    workspace: Path | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith(WORKSPACE_LINE_PREFIX):
            value = line[len(WORKSPACE_LINE_PREFIX):].strip()
            if value:
                workspace = Path(value).expanduser()
        elif session_id is None:
            session_id = line
    if not session_id:
        return None
    return CurrentSession(session_id=session_id, workspace=workspace)


def get_current_session() -> str | None:
    """Read the current session ID; return ``None`` when unset / the file is unreadable."""
    entry = get_current_session_entry()
    return entry.session_id if entry is not None else None


def get_current_session_workspace() -> Path | None:
    """Read the workspace recorded alongside the current session; ``None`` when unset / not recorded."""
    entry = get_current_session_entry()
    return entry.workspace if entry is not None else None


def set_current_session(session_id: str, *, workspace: str | Path | None = None) -> Path:
    """Set ``session_id`` (and optionally its ``workspace``) as the current session; return the written file path.

    ``workspace`` is optional so existing callers keep working; when given it is recorded on a second
    ``workspace=<path>`` line, enabling ``chat``'s fast "does this session belong to my workspace?" check.
    """
    session_id = str(session_id).strip()
    if not session_id:
        raise ValueError("session_id must not be empty")
    lines = [session_id]
    if workspace is not None:
        workspace_text = str(workspace).strip()
        if workspace_text:
            lines.append(f"{WORKSPACE_LINE_PREFIX}{Path(workspace_text).expanduser()}")
    path = current_session_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def clear_current_session() -> bool:
    """Clear the current session; return whether the file was actually deleted."""
    try:
        current_session_path().unlink()
        return True
    except OSError:
        return False
