"""SQLite storage: WAL + checkpoints + session catalogue in one connection.

``SqliteStorage`` subclasses :class:`~linteum_agent.bus.wal.SqliteWal`, so the
``events`` table and its group-commit path are reused unchanged.  Two extra
tables are added:

* ``checkpoints`` — the latest state snapshots (keep-N, ``Checkpoint.seq`` is the
  last applied bus event seq);
* ``sessions`` — the catalogue read model (list/open/delete).

One connection is used for all three tables: this keeps ``:memory:`` databases
coherent and lets the snapshot + catalogue update share one transaction.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Optional

from ..bus.wal import SqliteWal
from ..runtime.state.agent_state import AgentState
from ..runtime.state.checkpoint import Checkpoint, should_snapshot
from .base import SessionInfo

_EXTRA_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id    TEXT PRIMARY KEY,
    created_at    REAL NOT NULL,
    updated_at    REAL NOT NULL,
    phase         TEXT NOT NULL DEFAULT 'idle',
    round         INTEGER NOT NULL DEFAULT 0,
    message_count INTEGER NOT NULL DEFAULT 0,
    last_seq      INTEGER NOT NULL DEFAULT 0,
    title         TEXT,
    workspace     TEXT,
    metadata      TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS checkpoints (
    session_id TEXT NOT NULL,
    seq        INTEGER NOT NULL,
    version    INTEGER NOT NULL,
    phase      TEXT NOT NULL,
    state      TEXT NOT NULL,
    created    REAL NOT NULL,
    PRIMARY KEY (session_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_cp_session ON checkpoints(session_id, created);
"""

_CHECKPOINT_COLUMNS = "session_id, seq, version, phase, state, created"
_SESSION_COLUMNS = (
    "session_id, created_at, updated_at, phase, round, message_count, "
    "last_seq, title, workspace, metadata"
)


class SqliteStorage(SqliteWal):
    """Persistent ``WalStore`` + ``Checkpointer`` + ``SessionCatalog``."""

    def __init__(
        self,
        path: str | os.PathLike = ":memory:",
        *,
        keep: int = 3,
        every_n_events: int = 500,
        group_commit_ms: int = 5,
    ) -> None:
        if keep < 1:
            raise ValueError("keep must be >= 1")
        self.keep = keep
        self.every_n_events = max(1, every_n_events)
        super().__init__(path, group_commit_ms=group_commit_ms)
        with self._lock:
            self._conn.executescript(_EXTRA_SCHEMA)
            self._conn.commit()

    # -- Checkpointer -------------------------------------------------------
    def maybe_snapshot(self, state: AgentState, *, force: bool = False) -> Optional[Checkpoint]:
        if not should_snapshot(
            state, every_n_events=self.every_n_events, force=force
        ):
            return None
        created = time.time()
        checkpoint = Checkpoint(
            session_id=state.session_id,
            seq=state.last_event_seq,
            version=state.version,
            phase=state.phase,
            state=state.to_dict(),
            created=created,
        )
        self._write_checkpoint(checkpoint, state)
        return checkpoint

    def _write_checkpoint(self, checkpoint: Checkpoint, state: AgentState) -> None:
        encoded = json.dumps(
            checkpoint.state, ensure_ascii=False, allow_nan=False, sort_keys=True
        )
        with self._lock:
            self._conn.execute(
                f"INSERT OR REPLACE INTO checkpoints ({_CHECKPOINT_COLUMNS}) "
                "VALUES (?,?,?,?,?,?)",
                (
                    checkpoint.session_id,
                    checkpoint.seq,
                    checkpoint.version,
                    checkpoint.phase,
                    encoded,
                    checkpoint.created,
                ),
            )
            self._conn.execute(
                """
                INSERT INTO sessions
                    (session_id, created_at, updated_at, phase, round,
                     message_count, last_seq)
                VALUES (?,?,?,?,?,?,?)
                ON CONFLICT(session_id) DO UPDATE SET
                    updated_at=excluded.updated_at,
                    phase=excluded.phase,
                    round=excluded.round,
                    message_count=excluded.message_count,
                    last_seq=excluded.last_seq
                """,
                (
                    state.session_id,
                    checkpoint.created,
                    checkpoint.created,
                    state.phase,
                    state.round,
                    len(state.messages),
                    state.last_event_seq,
                ),
            )
            self._conn.execute(
                """
                DELETE FROM checkpoints
                WHERE session_id=?
                  AND rowid NOT IN (
                      SELECT rowid FROM checkpoints
                      WHERE session_id=?
                      ORDER BY created DESC, rowid DESC
                      LIMIT ?
                  )
                """,
                (checkpoint.session_id, checkpoint.session_id, self.keep),
            )
            self._conn.commit()

    def latest(self, session_id: str) -> Optional[Checkpoint]:
        with self._lock:
            row = self._conn.execute(
                f"SELECT {_CHECKPOINT_COLUMNS} FROM checkpoints "
                "WHERE session_id=? ORDER BY created DESC, rowid DESC LIMIT 1",
                (session_id,),
            ).fetchone()
        return self._checkpoint_from_row(row) if row is not None else None

    def all(self, session_id: str) -> list:
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {_CHECKPOINT_COLUMNS} FROM checkpoints "
                "WHERE session_id=? ORDER BY created, rowid",
                (session_id,),
            ).fetchall()
        return [self._checkpoint_from_row(row) for row in rows]

    def restore(self, session_id: str) -> Optional[AgentState]:
        checkpoint = self.latest(session_id)
        return AgentState.from_dict(checkpoint.state) if checkpoint is not None else None

    @staticmethod
    def _checkpoint_from_row(row: tuple) -> Checkpoint:
        return Checkpoint(
            session_id=row[0],
            seq=int(row[1]),
            version=int(row[2]),
            phase=row[3],
            state=json.loads(row[4]),
            created=float(row[5]),
        )

    # -- SessionCatalog -----------------------------------------------------
    async def upsert_session(self, info: SessionInfo) -> None:
        await asyncio.to_thread(self._upsert_session_sync, info)

    def _upsert_session_sync(self, info: SessionInfo) -> None:
        with self._lock:
            self._conn.execute(
                f"""
                INSERT INTO sessions ({_SESSION_COLUMNS})
                VALUES (?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(session_id) DO UPDATE SET
                    updated_at=excluded.updated_at,
                    phase=excluded.phase,
                    round=excluded.round,
                    message_count=excluded.message_count,
                    last_seq=excluded.last_seq,
                    title=COALESCE(excluded.title, sessions.title),
                    workspace=COALESCE(excluded.workspace, sessions.workspace),
                    metadata=excluded.metadata
                """,
                (
                    info.session_id,
                    info.created_at,
                    info.updated_at,
                    info.phase,
                    info.round,
                    info.message_count,
                    info.last_seq,
                    info.title,
                    info.workspace,
                    json.dumps(info.metadata or {}, ensure_ascii=False, allow_nan=False),
                ),
            )
            self._conn.commit()

    async def get_session(self, session_id: str) -> Optional[SessionInfo]:
        return await asyncio.to_thread(self._get_session_sync, session_id)

    def _get_session_sync(self, session_id: str) -> Optional[SessionInfo]:
        with self._lock:
            row = self._conn.execute(
                f"SELECT {_SESSION_COLUMNS} FROM sessions WHERE session_id=?",
                (session_id,),
            ).fetchone()
        return self._session_from_row(row) if row is not None else None

    async def list_sessions(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        workspace: str | None = None,
    ) -> list[SessionInfo]:
        return await asyncio.to_thread(
            self._list_sessions_sync, limit, offset, workspace
        )

    def _list_sessions_sync(
        self, limit: int, offset: int, workspace: str | None
    ) -> list[SessionInfo]:
        limit = max(1, int(limit))
        offset = max(0, int(offset))
        with self._lock:
            if workspace is None:
                rows = self._conn.execute(
                    f"SELECT {_SESSION_COLUMNS} FROM sessions "
                    "ORDER BY updated_at DESC, session_id ASC LIMIT ? OFFSET ?",
                    (limit, offset),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    f"SELECT {_SESSION_COLUMNS} FROM sessions "
                    "WHERE workspace=? "
                    "ORDER BY updated_at DESC, session_id ASC LIMIT ? OFFSET ?",
                    (workspace, limit, offset),
                ).fetchall()
        return [self._session_from_row(row) for row in rows]

    async def delete_session(self, session_id: str) -> None:
        await asyncio.to_thread(self._delete_session_sync, session_id)

    def _delete_session_sync(self, session_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM checkpoints WHERE session_id=?", (session_id,))
            self._conn.execute("DELETE FROM sessions WHERE session_id=?", (session_id,))
            self._conn.commit()

    @staticmethod
    def _session_from_row(row: tuple) -> SessionInfo:
        try:
            metadata = json.loads(row[9])
        except (TypeError, ValueError):
            metadata = {}
        return SessionInfo(
            session_id=row[0],
            created_at=float(row[1]),
            updated_at=float(row[2]),
            phase=row[3],
            round=int(row[4]),
            message_count=int(row[5]),
            last_seq=int(row[6]),
            title=row[7],
            workspace=row[8],
            metadata=metadata,
        )


__all__ = ["SqliteStorage"]
