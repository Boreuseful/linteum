"""Write-Ahead Log: durability before delivery (§14, P5).

The bus's ordering guarantee is *"先落盘再投递"* — nothing is queued for
dispatch until the append future has resolved.  Two backends are provided:

* :class:`InMemoryWal` — zero-dependency, used by tests and single-process demos.
* :class:`SqliteWal` — SQLite in WAL mode with **group commit**, the §14.2
  "single-machine development" choice.  Concurrent appends are batched into one
  transaction (one fsync) by a background committer.

The ``(session_id, seq)`` unique index is the storage side of idempotency
(§12) and the append-only scan is what recovery replays (§47).
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import threading
from collections import defaultdict
from typing import Iterable, Optional, Protocol, runtime_checkable

from .event import Event

# Column order is part of the contract with ``_to_row`` / ``_from_row``.
_COLUMNS: tuple = (
    "id", "type", "ts", "session_id", "seq", "trace_id", "cause_id",
    "origin", "priority", "ttl_ms", "schema_version", "payload",
)


@runtime_checkable
class WalStore(Protocol):
    """The durability surface the bus relies on."""

    async def append(self, ev: Event) -> None: ...

    async def append_many(self, evs: Iterable[Event]) -> None: ...

    async def fetch(self, event_id: str) -> Optional[Event]: ...

    async def replay(self, *, session_id: Optional[str] = None, after_seq: int = 0) -> list[Event]: ...

    async def last_seq(self, session_id: str) -> int: ...

    async def event_ids(self, session_id: str) -> list[str]: ...

    async def count(self) -> int: ...

    async def close(self) -> None: ...


class InMemoryWal:
    """Append-only in-process log with the same contract as :class:`SqliteWal`.

    ``append`` performs no ``await`` before mutating state, which preserves
    arrival order and therefore the session's ``seq`` monotonicity (I10).
    """

    def __init__(self) -> None:
        self._events: list[Event] = []
        self._by_id: dict[str, Event] = {}
        self._by_session: dict[str, list[Event]] = defaultdict(list)
        self._closed = False

    async def append(self, ev: Event) -> None:
        if ev.id in self._by_id:  # (session_id, seq) style idempotency
            return
        self._events.append(ev)
        self._by_id[ev.id] = ev
        self._by_session[ev.session_id].append(ev)

    async def append_many(self, evs: Iterable[Event]) -> None:
        for ev in evs:
            await self.append(ev)

    async def fetch(self, event_id: str) -> Optional[Event]:
        return self._by_id.get(event_id)

    async def replay(
        self, *, session_id: Optional[str] = None, after_seq: int = 0
    ) -> list[Event]:
        if session_id is None:
            return list(self._events)
        return [ev for ev in self._by_session.get(session_id, ()) if ev.seq > after_seq]

    async def last_seq(self, session_id: str) -> int:
        events = self._by_session.get(session_id, ())
        return max((ev.seq for ev in events), default=0)

    async def event_ids(self, session_id: str) -> list[str]:
        return [ev.id for ev in self._by_session.get(session_id, ())]

    async def count(self) -> int:
        return len(self._events)

    async def close(self) -> None:
        self._closed = True

    def __len__(self) -> int:
        return len(self._events)


# ---------------------------------------------------------------------------
# SQLite backend
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id             TEXT PRIMARY KEY,
    type           TEXT NOT NULL,
    ts             REAL NOT NULL,
    session_id     TEXT NOT NULL,
    seq            INTEGER NOT NULL,
    trace_id       TEXT,
    cause_id       TEXT,
    origin         TEXT NOT NULL,
    priority       INTEGER NOT NULL,
    ttl_ms         INTEGER,
    schema_version INTEGER NOT NULL,
    payload        TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_events_session_seq
    ON events (session_id, seq);
CREATE INDEX IF NOT EXISTS idx_events_type ON events (type);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events (ts);
"""

_INSERT = (
    f"INSERT OR IGNORE INTO events ({', '.join(_COLUMNS)}) "
    f"VALUES ({', '.join('?' * len(_COLUMNS))})"
)


class SqliteWal:
    """SQLite (WAL-mode) log with background group commit.

    Every ``append`` synchronously enqueues a ``(event, future)`` pair — the
    synchronous enqueue is what keeps ``seq`` order aligned with log order even
    when many coroutines append concurrently — then awaits the future, which a
    single committer task resolves once the batch is durably committed.
    """

    def __init__(self, path: str | os.PathLike = ":memory:", *, group_commit_ms: int = 5) -> None:
        self.path = str(path)
        self.group_commit_ms = max(0.0, group_commit_ms / 1000.0)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        self._lock = threading.Lock()
        self._pending: list[tuple[Event, asyncio.Future]] = []
        self._flush_evt = asyncio.Event()
        self._committer: Optional[asyncio.Task] = None
        self._closed = False

    # -- row mapping --------------------------------------------------------
    @staticmethod
    def _to_row(ev: Event) -> tuple:
        return (
            ev.id, ev.type, ev.ts, ev.session_id, ev.seq, ev.trace_id,
            ev.cause_id, ev.origin, ev.priority, ev.ttl_ms, ev.schema_version,
            json.dumps(ev.payload, ensure_ascii=False, allow_nan=False),
        )

    @staticmethod
    def _from_row(row: tuple) -> Event:
        return Event(
            id=row[0], type=row[1], ts=row[2], session_id=row[3], seq=row[4],
            trace_id=row[5], cause_id=row[6], origin=row[7], priority=row[8],
            ttl_ms=row[9], schema_version=row[10], payload=json.loads(row[11]),
        )

    # -- append pipeline ----------------------------------------------------
    def _ensure_committer(self) -> None:
        if self._committer is None or self._committer.done():
            self._committer = asyncio.get_running_loop().create_task(self._run_committer())

    async def append(self, ev: Event) -> None:
        self._ensure_committer()
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending.append((ev, fut))  # sync enqueue => order preserved
        self._flush_evt.set()
        await fut

    async def append_many(self, evs: Iterable[Event]) -> None:
        items = list(evs)
        if not items:
            return
        self._ensure_committer()
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        for ev in items:
            self._pending.append((ev, fut))  # one fsync for the whole batch
        self._flush_evt.set()
        await fut

    async def _run_committer(self) -> None:
        try:
            while True:
                if not self._pending:
                    self._flush_evt.clear()
                    await self._flush_evt.wait()
                await asyncio.sleep(self.group_commit_ms)
                await self._flush_pending()
        except asyncio.CancelledError:
            await self._flush_pending()
            raise

    async def _flush_pending(self) -> None:
        if not self._pending:
            return
        batch = self._pending
        self._pending = []
        rows = [self._to_row(ev) for ev, _ in batch]
        try:
            await asyncio.to_thread(self._write_rows, rows)
        except Exception as exc:  # surface the failure to every waiting appender
            for _, fut in batch:
                if not fut.done():
                    fut.set_exception(exc)
            return
        for _, fut in batch:
            if not fut.done():
                fut.set_result(None)

    def _write_rows(self, rows: list[tuple]) -> None:
        with self._lock:
            self._conn.executemany(_INSERT, rows)
            self._conn.commit()

    # -- reads --------------------------------------------------------------
    async def fetch(self, event_id: str) -> Optional[Event]:
        row = await asyncio.to_thread(self._fetch_sync, event_id)
        return self._from_row(row) if row is not None else None

    def _fetch_sync(self, event_id: str) -> Optional[tuple]:
        with self._lock:
            return self._conn.execute(
                f"SELECT {', '.join(_COLUMNS)} FROM events WHERE id=?", (event_id,)
            ).fetchone()

    async def replay(
        self, *, session_id: Optional[str] = None, after_seq: int = 0
    ) -> list[Event]:
        await self._flush_pending()  # make sure everything is visible
        rows = await asyncio.to_thread(self._replay_sync, session_id, after_seq)
        return [self._from_row(r) for r in rows]

    def _replay_sync(self, session_id: Optional[str], after_seq: int) -> list[tuple]:
        cols = ", ".join(_COLUMNS)
        with self._lock:
            if session_id is None:
                cur = self._conn.execute(f"SELECT {cols} FROM events ORDER BY ts, seq")
            else:
                cur = self._conn.execute(
                    f"SELECT {cols} FROM events WHERE session_id=? AND seq>? ORDER BY seq",
                    (session_id, after_seq),
                )
            return cur.fetchall()

    async def last_seq(self, session_id: str) -> int:
        await self._flush_pending()
        return await asyncio.to_thread(self._last_seq_sync, session_id)

    def _last_seq_sync(self, session_id: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT MAX(seq) FROM events WHERE session_id=?", (session_id,)
            ).fetchone()
        return int(row[0]) if row and row[0] is not None else 0

    async def event_ids(self, session_id: str) -> list[str]:
        await self._flush_pending()
        return await asyncio.to_thread(self._event_ids_sync, session_id)

    def _event_ids_sync(self, session_id: str) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id FROM events WHERE session_id=? ORDER BY seq",
                (session_id,),
            ).fetchall()
        return [row[0] for row in rows]

    async def count(self) -> int:
        return await asyncio.to_thread(self._count_sync)

    def _count_sync(self) -> int:
        with self._lock:
            return int(self._conn.execute("SELECT COUNT(*) FROM events").fetchone()[0])

    # -- lifecycle ----------------------------------------------------------
    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._flush_pending()
        task, self._committer = self._committer, None
        if task is not None:
            if not task.done():
                task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        await asyncio.to_thread(self._close_sync)

    def _close_sync(self) -> None:
        with self._lock:
            self._conn.close()
