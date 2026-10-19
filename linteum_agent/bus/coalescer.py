"""Delta coalescing: 50 ms / 4 KB windows (§13.1, Checklist #9).

``llm.token.delta`` and ``tool.output.delta`` are the highest-frequency events on
the bus; delivering every token as its own event would (a) dominate handler CPU
and (b) turn a chatty stream into bus backpressure.  The coalescer buffers
deltas that share a ``(session_id, type, call_id)`` key and emits a single
merged event per window, or immediately when the byte cap is hit.

Merging is *order-preserving* and identity-safe: only known text keys
(``text`` / ``delta`` / ``content`` / ...) are concatenated and only known
counter keys sum; every other field (``call_id``, ``name``, ``index``, ...) keeps
the **first** event's value so identity is never corrupted.  The merged event
keeps the first event's identity (``id`` / ``trace_id`` / ``cause_id``) so causal
chains stay intact — the bus assigns the single ``seq`` at commit time, which is
why absorbed deltas never consume a seq.
"""

from __future__ import annotations

import time
from dataclasses import replace
from typing import Callable, Hashable

from .event import Event, payload_size

DEFAULT_WINDOW_MS = 50
DEFAULT_MAX_BYTES = 4096

#: String fields that are concatenated across a delta window.
CONCAT_KEYS: frozenset = frozenset(
    {
        "text",
        "delta",
        "content",
        "reasoning",
        "reasoning_content",
        "output",
        "chunk",
        "arguments",
        "partial_json",
    }
)
#: Numeric fields that are summed across a delta window.
SUM_KEYS: frozenset = frozenset({"tokens", "token_count", "bytes"})

_NUMBER = (int, float)


class _DeltaGroup:
    """Accumulator for one ``(session_id, type, call_id)`` delta stream."""

    __slots__ = ("base", "deadline", "payload", "count")

    def __init__(self, base: Event, deadline: float) -> None:
        self.base = base
        self.deadline = deadline
        self.payload: dict = dict(base.payload)
        self.count = 1

    def merge(self, ev: Event) -> None:
        for key, value in ev.payload.items():
            if key not in self.payload:
                self.payload[key] = value
                continue
            current = self.payload[key]
            if key in CONCAT_KEYS and isinstance(current, str) and isinstance(value, str):
                self.payload[key] = current + value
            elif (
                key in SUM_KEYS
                and isinstance(current, _NUMBER)
                and isinstance(value, _NUMBER)
                and not isinstance(current, bool)
                and not isinstance(value, bool)
            ):
                self.payload[key] = current + value
            # otherwise: keep the first value (identity / constant fields)
        self.count += 1

    def size(self) -> int:
        return payload_size(self.payload)

    def build(self) -> Event:
        if self.count <= 1:
            return self.base
        merged = dict(self.payload)
        merged["coalesced_count"] = self.count
        return replace(self.base, payload=merged)


class DeltaCoalescer:
    """Time + size windowed merger for ``*.delta`` events."""

    def __init__(
        self,
        window_ms: int = DEFAULT_WINDOW_MS,
        max_bytes: int = DEFAULT_MAX_BYTES,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.window = max(0.0, window_ms / 1000.0)
        self.max_bytes = max(1, max_bytes)
        self._clock = clock
        self._groups: dict[Hashable, _DeltaGroup] = {}

    # -- intake -------------------------------------------------------------
    def maybe_merge(self, ev: Event) -> Event | None:
        """Return the event to publish now, or ``None`` if it was buffered.

        Non-delta events pass straight through.  A delta whose group exceeds the
        byte cap is flushed immediately (returning the merged event).
        """
        if not ev.type.endswith(".delta"):
            return ev

        key = (ev.session_id, ev.type, ev.payload.get("call_id"))
        group = self._groups.get(key)
        if group is None:
            self._groups[key] = _DeltaGroup(ev, self._clock() + self.window)
            return None

        group.merge(ev)
        if group.size() >= self.max_bytes:
            del self._groups[key]
            return group.build()
        return None

    # -- draining -----------------------------------------------------------
    def flush_due(self) -> list[Event]:
        """Flush every group whose window has elapsed."""
        now = self._clock()
        due = [k for k, g in self._groups.items() if g.deadline <= now]
        return self._pop(due)

    def flush_all(self) -> list[Event]:
        """Flush all buffered groups (used on shutdown)."""
        return self._pop(list(self._groups.keys()))

    def _pop(self, keys: list[Hashable]) -> list[Event]:
        out: list[Event] = []
        for key in keys:
            group = self._groups.pop(key, None)
            if group is not None:
                out.append(group.build())
        return out

    def next_delay(self) -> float | None:
        """Seconds until the earliest buffered group is due.

        ``None`` means nothing is buffered; ``0.0`` means flush immediately.
        The dispatch loop uses this to sleep until the window expires instead
        of polling.
        """
        if not self._groups:
            return None
        deadline = min(group.deadline for group in self._groups.values())
        return max(0.0, deadline - self._clock())

    @property
    def pending(self) -> int:
        return len(self._groups)
