"""The current-session pointer.

`linteum sessions new/switch` writes the selected session ID into ``~/.agent/current_session``;
`linteum chat` **reuses** that session by default when ``-s/--session`` is not explicitly given.

- Default location: ``~/.agent/current_session`` (a text file containing only the session ID);
- The environment variable ``linteum_agent_CURRENT_SESSION`` can override the file path (tests /
  multiple projects);
- Both reading and writing tolerate a missing file: reading returns ``None`` (meaning "no current
  session set") and never raises.
"""

from __future__ import annotations

import os
from pathlib import Path

#: Current session file name (located in the user-level ``~/.agent``).
CURRENT_SESSION_FILENAME = "current_session"
#: Environment variable: overrides the current session file path.
CURRENT_SESSION_PATH_ENV = "linteum_agent_CURRENT_SESSION"


def current_session_path() -> Path:
    """The current session file path (the ``linteum_agent_CURRENT_SESSION`` env var can override it)."""
    override = os.getenv(CURRENT_SESSION_PATH_ENV)
    if override and override.strip():
        return Path(override).expanduser()
    from cli.snapshot import agent_dir

    return agent_dir() / CURRENT_SESSION_FILENAME


def get_current_session() -> str | None:
    """Read the current session ID; return ``None`` when unset / the file is unreadable."""
    try:
        text = current_session_path().read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return text or None


def set_current_session(session_id: str) -> Path:
    """Set ``session_id`` as the current session and return the written file path."""
    session_id = str(session_id).strip()
    if not session_id:
        raise ValueError("session_id must not be empty")
    path = current_session_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(session_id + "\n", encoding="utf-8")
    return path


def clear_current_session() -> bool:
    """Clear the current session; return whether the file was actually deleted."""
    try:
        current_session_path().unlink()
        return True
    except OSError:
        return False
