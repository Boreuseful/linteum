from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from threading import RLock
from typing import Any, Iterable, Mapping

try:
    from ..messages.agent_message import Message, MessageInput, MessageInputs, normalize_messages
    from .session import Session
except ImportError:  # supports adding /workspace/linteum_agent to sys.path directly
    from messages.agent_message import Message, MessageInput, MessageInputs, normalize_messages
    from session import Session


class ContextStore:
    """Store ``Session`` objects by session (workspace + context representation + Tool/Skill config + usage) with JSON persistence.

    The persistence format is::

        {
          "version": 4,
          "sessions": {
            "demo-session": {
              "session_id": "demo-session",
              "workspace": "/workspace",
              "metadata": {"owner": "alice"},
              "tools": {"registered": ["read_file"], "disabled": ["bash"]},
              "skills": {"registered": ["python-code-review"]},
              "usage": {"input_tokens": 150, "output_tokens": 30,
                        "cache_hit_tokens": 45, "total_tokens": 180},
              "created_at": "2026-01-01T00:00:00+00:00",
              "updated_at": "2026-01-01T00:00:00+00:00",
              "messages": [
                {
                  "round": 1,
                  "context": [
                    {"role": "user", "content": "hello"},
                    {"role": "assistant", "content": "hello, how can I help you?"}
                  ],
                  "usage": {
                    "input_tokens": 12, "output_tokens": 8,
                    "cache_hit_tokens": 0, "total_tokens": 20,
                    "context_tokens": 20, "context_chars": 42,
                    "created_at": "2026-01-01T00:00:00+00:00",
                    "updated_at": "2026-01-01T00:00:05+00:00"
                  }
                }
              ]
            }
          }
        }

    ``messages`` is a **list of rounds**: each round (= one complete agent-loop) is wrapped as
    ``{round, context, usage}``, where ``context`` is all of this round's messages (request + every LLM
    call + tool results) and ``usage`` stores this round's input/output token summary, the context space
    this round occupies (``context_tokens`` / ``context_chars``), and this round's start/end time
    (``created_at`` / ``updated_at``).

    ``tools`` / ``skills`` / ``usage`` are top-level fields of each session, siblings of ``metadata``.

    Backward compatibility:

    - ``version=3``: ``messages`` is a flat message list (``tools`` / ``skills`` / ``usage`` are already
      separate fields); on read it is split into rounds "starting at each user message";
    - ``version=2``: ``tools`` / ``skills`` / ``usage`` are embedded in ``metadata`` and are migrated to
      separate fields on read;
    - ``version=1``: ``sessions`` is directly a message list; after loading, the session's ``workspace``
      depends on the store's ``default_workspace`` (``None`` when unconfigured, inheriting the tool
      registry's workspace at runtime).

    Saving always writes the latest format (version 4), so reading an old file once completes the format
    upgrade.

    It does not use pickle, so the generated content can be inspected, migrated, or read by other
    services directly.
    """

    VERSION = 4
    LEGACY_VERSION = 1
    # version 2: Tool/Skill config and usage were once embedded in ``metadata``.
    METADATA_VERSION = 2
    # version 3: ``messages`` is a flat message list (not yet wrapped by round).
    MESSAGES_VERSION = 3
    READABLE_VERSIONS = (VERSION, MESSAGES_VERSION, METADATA_VERSION, LEGACY_VERSION)

    def __init__(
        self,
        initial: dict[str, MessageInputs] | None = None,
        *,
        workspace: str | Path | None = None,
    ):
        self._sessions: dict[str, Session] = {}
        self._lock = RLock()
        # The default workspace used when creating a new session; None means "inherited at runtime".
        self.default_workspace: Path | None = (
            None if workspace is None else Path(workspace).expanduser().resolve()
        )
        for session_id, messages in (initial or {}).items():
            self.ensure_session(session_id).replace(messages)

    # ------------------------------------------------------------------
    # Session-level read/write
    # ------------------------------------------------------------------

    def ensure_session(
        self,
        session_id: str = "default",
        *,
        workspace: str | Path | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> Session:
        """Get the session, creating it if it does not exist. An existing session is not overwritten.

        ``workspace`` only takes effect when the session does not exist, or when the session has not yet
        specified a workspace; to modify an existing session's workspace use ``set_workspace()``.
        """
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                session = Session(
                    session_id=session_id,
                    workspace=self.default_workspace if workspace is None else workspace,
                    metadata=dict(metadata or {}),
                )
                self._sessions[session_id] = session
                return session
            if workspace is not None and session.workspace is None:
                session.set_workspace(workspace)
            if metadata:
                session.update_metadata(metadata)
            return session

    def get_session(self, session_id: str = "default") -> Session | None:
        """Return the session object; ``None`` if it does not exist (does not create a session)."""
        with self._lock:
            return self._sessions.get(session_id)

    def set_workspace(self, session_id: str, workspace: str | Path | None) -> Session:
        """Set the session workspace; ``None`` restores "inherited at runtime"."""
        with self._lock:
            return self.ensure_session(session_id).set_workspace(workspace)

    def workspace(self, session_id: str = "default") -> Path:
        """Return the session's effective workspace; falls back to the default workspace or ``Path.cwd()`` when the session does not exist."""
        session = self.get_session(session_id)
        if session is not None:
            return session.effective_workspace
        return self.default_workspace or Path.cwd()

    # ------------------------------------------------------------------
    # Legacy-compatible interface: read/write the message list directly (equivalent to operating on session.context)
    # ------------------------------------------------------------------

    def get(self, session_id: str = "default") -> list[Message]:
        with self._lock:
            session = self._sessions.get(session_id)
            return session.messages() if session is not None else []

    def replace(self, session_id: str, messages: MessageInputs) -> None:
        with self._lock:
            self.ensure_session(session_id).replace(messages)

    def append(self, session_id: str, messages: MessageInput | Iterable[MessageInput]) -> None:
        with self._lock:
            self.ensure_session(session_id).append(messages)

    def clear(self, session_id: str = "default") -> None:
        with self._lock:
            self._sessions.pop(session_id, None)

    def clear_all(self) -> None:
        with self._lock:
            self._sessions.clear()

    def sessions(self) -> list[str]:
        with self._lock:
            return list(self._sessions)

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        with self._lock:
            return {
                "version": self.VERSION,
                "sessions": {
                    session_id: session.to_dict()
                    for session_id, session in self._sessions.items()
                },
            }

    def dumps(self, *, indent: int | None = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)

    def loads(self, value: str | bytes, *, merge: bool = False) -> None:
        payload = json.loads(value)
        if not isinstance(payload, dict):
            raise ValueError("the context JSON root must be an object")
        version = payload.get("version", self.LEGACY_VERSION)
        if version not in self.READABLE_VERSIONS:
            raise ValueError(f"unsupported context version: {version}")
        sessions = payload.get("sessions", {})
        if not isinstance(sessions, dict):
            raise ValueError("the context JSON 'sessions' must be an object")
        if merge:
            self.merge(sessions)
        else:
            self.replace_all(sessions)

    def merge(self, sessions: Mapping[str, Any]) -> None:
        """**Merge** the sessions from a snapshot into the current store: the same id is overwritten, others are kept.

        Difference from ``replace_all()``: the latter discards existing sessions in the store, the former
        does not. ``AgentRunner`` uses this method when loading a session, to avoid wiping the memory of
        other sessions along with it.
        """
        with self._lock:
            for session_id, value in sessions.items():
                key = str(session_id)
                self._sessions[key] = self._coerce_session(key, value)

    def replace_all(self, sessions: Mapping[str, Any]) -> None:
        """Atomically replace all sessions.

        The value may be a ``Session``, a session snapshot from ``to_dict()``, or a legacy-format message
        collection.
        """
        with self._lock:
            self._sessions = {
                session_id: self._coerce_session(session_id, value)
                for session_id, value in sessions.items()
            }

    def _coerce_session(self, session_id: str, value: Any) -> Session:
        if isinstance(value, Session):
            return value if value.session_id == session_id else replace(value, session_id=session_id)
        if isinstance(value, dict) and "role" not in value:
            return Session.from_dict(session_id, value)
        return Session(
            session_id=session_id,
            workspace=self.default_workspace,
            context=normalize_messages(value),
        )

    def save(self, path: str | Path) -> None:
        Path(path).write_text(self.dumps(), encoding="utf-8")

    def save_session(self, path: str | Path, session_id: str) -> None:
        """Write only the one session ``session_id`` to the file (one file = one session).

        Difference from ``save()``: the latter writes out **all** sessions in the store. Following the
        ``.agent/sessions/<session_id>.json`` naming convention, the caller usually only needs the current
        session, so other sessions (such as a just-constructed empty session) are not dragged into this
        file.
        """
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                raise KeyError(f"session does not exist: {session_id}")
            payload = {
                "version": self.VERSION,
                "sessions": {session_id: session.to_dict()},
            }
        Path(path).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def load(self, path: str | Path, *, merge: bool = False) -> None:
        """Read sessions from a file; with ``merge=True`` keep the other sessions already in the store."""
        self.loads(Path(path).read_text(encoding="utf-8"), merge=merge)


__all__ = ["ContextStore", "Session"]
