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
    """Semaphore-gated task spawner with per-session serialisation gates.

    Two lanes share the pool's bookkeeping but have independent concurrency
    budgets:

    * ``dispatch`` (default) — event handlers.  A session's handlers use at
      most ``max_per_session`` slots.
    * ``execution`` — long-running tool work.  It gets its own global and
      per-session gates so a blocked tool can never starve event dispatch
      (async ReAct AR-16).  ``ToolRuntime`` submits its workers here.

    Both lanes still contribute to ``tasks`` / ``inflight``, so
    ``bus.wait_until_idle()`` keeps covering tool work.
    """

    def __init__(
        self,
        size: int = DEFAULT_SIZE,
        max_per_session: int = DEFAULT_MAX_PER_SESSION,
        *,
        execution_size: Optional[int] = None,
        execution_per_session: Optional[int] = None,
    ) -> None:
        if size <= 0:
            raise ValueError("worker pool size must be positive")
        if max_per_session <= 0:
            raise ValueError("per-session limit must be positive")
        exec_size = size if execution_size is None else execution_size
        exec_per_session = (
            max_per_session if execution_per_session is None
            else execution_per_session
        )
        if exec_size <= 0 or exec_per_session <= 0:
            raise ValueError("execution lane limits must be positive")
        self.size = size
        self.max_per_session = max_per_session
        self.execution_size = exec_size
        self.execution_per_session = exec_per_session
        self._sem = asyncio.Semaphore(size)
        self._per_session: dict[str, asyncio.Semaphore] = defaultdict(
            lambda: asyncio.Semaphore(max_per_session)
        )
        self._exec_sem = asyncio.Semaphore(exec_size)
        self._per_session_exec: dict[str, asyncio.Semaphore] = defaultdict(
            lambda: asyncio.Semaphore(exec_per_session)
        )
        self.tasks: set[asyncio.Task] = set()

    def spawn(
        self,
        coro: Awaitable,
        session_id: Optional[str] = None,
        *,
        lane: str = "dispatch",
    ) -> asyncio.Task:
        """Schedule ``coro`` on ``lane``; returns the task (referenced until done)."""
        if lane not in ("dispatch", "execution"):
            raise ValueError(f"unknown worker lane: {lane!r}")
        global_gate = self._sem if lane == "dispatch" else self._exec_sem
        session_gates = (
            self._per_session if lane == "dispatch" else self._per_session_exec
        )

        async def _run():
            async with global_gate:
                if session_id is None:
                    return await coro
                async with session_gates[session_id]:
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
