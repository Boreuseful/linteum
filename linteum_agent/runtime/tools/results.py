"""ResultStore: per-``call_id`` idempotency for tool execution (§12, §24).

The bus is at-least-once, so a redelivered ``tool.call.requested`` must not run
a side-effecting tool twice — "转账会转两次".  The runtime therefore consults the
result store *before* executing (``get``) and records the outcome *after*
(``put``); the ``(call_id)`` primary key makes the write an upsert
(``INSERT OR IGNORE``) so a duplicate result is a no-op (Checklist #8/#16).

Two backends ship here: an in-memory store for tests and a SQLite store for the
single-machine default (mirroring :class:`linteum_agent.bus.wal.SqliteWal`).
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from typing import Optional, Protocol, runtime_checkable


@runtime_checkable
class ResultStore(Protocol):
    """The narrow surface the tool runtime depends on."""

    async def get(self, call_id: str) -> Optional[dict]: ...

    async def put(self, call_id: str, result: dict) -> None: ...


class InMemoryResultStore:
    """Process-local result table (default for tests and single-process demos)."""

    def __init__(self) -> None:
        self._results: dict[str, dict] = {}

    async def get(self, call_id: str) -> Optional[dict]:
        cached = self._results.get(call_id)
        return json.loads(json.dumps(cached)) if cached is not None else None

    async def put(self, call_id: str, result: dict) -> None:
        # INSERT OR IGNORE semantics: the first result wins.
        self._results.setdefault(call_id, json.loads(json.dumps(result)))

    def clear(self) -> None:
        self._results.clear()

    def __len__(self) -> int:
        return len(self._results)


class NullResultStore:
    """A store that remembers nothing — effectively disables dedup.

    Useful when a host guarantees exactly-once delivery upstream; the runtime
    still calls ``get``/``put`` symmetrically so wiring stays uniform.
    """

    async def get(self, call_id: str) -> Optional[dict]:
        return None

    async def put(self, call_id: str, result: dict) -> None:
        return None


class SqliteResultStore:
    """Durable result table backed by SQLite (WAL mode), one row per ``call_id``."""

    _SCHEMA = """
    CREATE TABLE IF NOT EXISTS tool_results (
        call_id  TEXT PRIMARY KEY,
        result   TEXT NOT NULL,
        ts       REAL NOT NULL
    );
    """

    def __init__(self, path: str = ":memory:") -> None:
        self.path = path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(self._SCHEMA)
        self._conn.commit()

    async def get(self, call_id: str) -> Optional[dict]:
        return await asyncio.to_thread(self._get_sync, call_id)

    def _get_sync(self, call_id: str) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute(
                "SELECT result FROM tool_results WHERE call_id=?", (call_id,)
            ).fetchone()
        return json.loads(row[0]) if row else None

    async def put(self, call_id: str, result: dict) -> None:
        await asyncio.to_thread(self._put_sync, call_id, result)

    def _put_sync(self, call_id: str, result: dict) -> None:
        import time

        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO tool_results(call_id, result, ts) VALUES (?,?,?)",
                (call_id, json.dumps(result, ensure_ascii=False), time.time()),
            )
            self._conn.commit()

    async def close(self) -> None:
        await asyncio.to_thread(self._close_sync)

    def _close_sync(self) -> None:
        with self._lock:
            self._conn.close()
