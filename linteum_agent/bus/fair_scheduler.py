"""Per-origin bounded queues + weighted fair scheduling (§13.2, I9).

A single shared queue suffers head-of-line blocking: one chatty plugin parks its
own events in front of core events and the whole agent stalls.  The fix is one
bounded queue **per origin** drained by deficit round-robin, with ``core``
weighted 8× a plugin.  Combined with the "plugin queue full ⇒ shed" rule this
gives the headline isolation property:

    **I9** — a single origin flooding cannot move the ``core`` watermark.

Plugin overflow is dropped (never backpressured) precisely so that a misbehaving
third party can only hurt itself; core overflow blocks its producer instead.
"""

from __future__ import annotations

import asyncio
from collections import deque
from typing import Optional

from .event import Event
from .metrics import Metrics

DEFAULT_PER_ORIGIN_CAP = 2_000

#: Weight table (§13.2).  Plugins get the lowest share on purpose.
WEIGHTS: dict = {"core": 8, "channel:*": 4, "plugin:*": 1}


def _weight_for(origin: str, default: int) -> int:
    if origin == "core":
        return WEIGHTS["core"]
    if origin.startswith("channel:"):
        return WEIGHTS["channel:*"]
    if origin.startswith("plugin:"):
        return WEIGHTS["plugin:*"]
    return default


class OriginFairScheduler:
    """Deficit round-robin over per-origin bounded deques."""

    def __init__(
        self,
        per_origin_cap: int = DEFAULT_PER_ORIGIN_CAP,
        *,
        default_weight: int = 1,
        metrics: Optional[Metrics] = None,
    ) -> None:
        if per_origin_cap <= 0:
            raise ValueError("per_origin_cap must be positive")
        self.cap = per_origin_cap
        self.default_weight = default_weight
        self.metrics = metrics
        self._q: dict[str, deque] = {}
        self._space: dict[str, asyncio.Event] = {}
        self._current: Optional[str] = None
        self._remaining: int = 0

    # -- enqueue ------------------------------------------------------------
    async def put(self, ev: Event) -> bool:
        """Enqueue ``ev``; return False if it was shed (plugin overflow)."""
        q = self._q.setdefault(ev.origin, deque())
        if len(q) >= self.cap:
            if ev.origin.startswith("plugin:"):
                self._shed(ev)
                return False
            await self._wait_space(ev.origin)  # core / channel: backpressure
            q = self._q.setdefault(ev.origin, deque())
        q.append(ev)
        return True

    async def _wait_space(self, origin: str) -> None:
        event = self._space.setdefault(origin, asyncio.Event())
        while len(self._q.get(origin, ())) >= self.cap:
            event.clear()
            await event.wait()

    def _shed(self, ev: Event) -> None:
        if self.metrics is not None:
            self.metrics.shed(ev.origin, ev.type)

    # -- dequeue (sync, single-threaded event loop) -------------------------
    def next(self) -> Optional[Event]:
        """Pop the next event by deficit round-robin, or ``None`` if all empty.

        Each origin is served up to ``weight`` times consecutively (``core`` 8×, a
        plugin 1×), then the rotation advances.  That is real weighted fairness:
        a background plugin's events still get through, but core keeps the lion's
        share of the bus even under sustained plugin traffic (I9).
        """
        active = [o for o in self._q if self._q[o]]
        if not active:
            self._current = None
            self._remaining = 0
            return None

        current = self._current
        if current in active and self._remaining > 0:
            self._remaining -= 1
            return self._pop(current)

        start = active.index(current) + 1 if current in active else 0
        for step in range(len(active)):
            origin = active[(start + step) % len(active)]
            self._current = origin
            self._remaining = _weight_for(origin, self.default_weight) - 1
            return self._pop(origin)
        return None

    def _pop(self, origin: str) -> Event:
        ev = self._q[origin].popleft()
        space = self._space.get(origin)
        if space is not None:
            space.set()
        return ev

    # -- introspection ------------------------------------------------------
    def qsize(self, origin: str) -> int:
        return len(self._q.get(origin, ()))

    def total(self) -> int:
        return sum(len(q) for q in self._q.values())

    def capacity(self) -> int:
        return self.cap * max(1, len(self._q))

    def pressure(self) -> float:
        """Fullest-origin occupancy as a 0..1 fraction (drives shed decisions)."""
        if not self._q:
            return 0.0
        return max((len(q) for q in self._q.values()), default=0) / self.cap

    def origins(self) -> tuple[str, ...]:
        return tuple(self._q.keys())
