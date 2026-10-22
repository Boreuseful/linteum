"""Bounded worker pool: partition-concurrency for handler dispatch (§16.1).

One event loop, many partitions.  Each session is a partition and runs serially
(its handlers never overlap), while different sessions run concurrently up to a
global ceiling.  That is exactly the shape the bus needs: a session's
``llm.*`` / ``tool.*`` handlers observe a strictly serialised stream (so no
context locking is needed, §11), yet cross-session traffic is naturally parallel.

The bus only depends on ``spawn`` / ``drain``; the cancel registry and priority
arbitration of §16.2–16.4 arrive with the scheduler stage (S2).
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import Awaitable, Optional

DEFAULT_SIZE = 8
DEFAULT_MAX_PER_SESSION = 4


class WorkerPool:
    """Semaphore-gated task spawner with a per-session serialisation gate."""

    def __init__(self, size: int = DEFAULT_SIZE, max_per_session: int = DEFAULT_MAX_PER_SESSION) -> None:
        if size <= 0:
            raise ValueError("worker pool size must be positive")
        self.size = size
        self.max_per_session = max_per_session
        self._sem = asyncio.Semaphore(size)
        self._per_session: dict[str, asyncio.Semaphore] = defaultdict(
            lambda: asyncio.Semaphore(max_per_session)
        )
        self.tasks: set[asyncio.Task] = set()

    def spawn(self, coro: Awaitable, session_id: Optional[str] = None) -> asyncio.Task:
        """Schedule ``coro``; returns the task (kept referenced until done)."""

        async def _run():
            async with self._sem:
                if session_id is None:
                    return await coro
                async with self._per_session[session_id]:
                    return await coro

        task = asyncio.create_task(_run())
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    async def drain(self, timeout: float = 30.0) -> None:
        """Wait for in-flight tasks (best-effort, bounded by ``timeout``)."""
        if not self.tasks:
            return
        await asyncio.wait(set(self.tasks), timeout=timeout)

    @property
    def inflight(self) -> int:
        return len(self.tasks)
