"""Minimal observability collector for the bus (§48).

The design calls for counters to be incremented at well-defined points in the
publish pipeline (``published`` / ``dup`` / ``shed`` / ``namespace_violation``)
and for a bounded ring of recent events so that a tracer can render a causal
tree.  A full metrics stack (Prometheus / OTel) is a later concern; this module
deliberately keeps a dependency-free, in-process implementation with a tiny,
stable API that can be swapped out through injection.
"""

from __future__ import annotations

from collections import Counter, deque
from dataclasses import dataclass, field


@dataclass
class Metrics:
    """In-process counters + a bounded ring of recent labelled events.

    All methods are cheap (dict increments) so they can be called on the hot
    path without an ``await``.  ``snapshot`` returns a plain dict for tests.
    """

    recent_cap: int = 256
    counters: Counter = field(default_factory=Counter)
    recent: deque = field(default_factory=deque)

    def __post_init__(self) -> None:
        # ``recent_cap`` is authoritative; rebuild the ring with the right bound.
        self.recent = deque(self.recent, maxlen=max(1, self.recent_cap))

    # ── internal ──────────────────────────────────────────────────────────
    def incr(self, name: str, **labels: object) -> None:
        self.counters[name] += 1
        self.recent.append((name, labels))

    # ── public counters (names are part of the contract) ──────────────────
    def published(self, origin: str, etype: str) -> None:
        self.incr("published", origin=origin, type=etype)

    def delivered(self, origin: str, etype: str) -> None:
        self.incr("delivered", origin=origin, type=etype)

    def dup(self, etype: str) -> None:
        self.incr("dup", type=etype)

    def shed(self, origin: str, etype: str) -> None:
        self.incr("shed", origin=origin, type=etype)

    def coalesced(self, etype: str) -> None:
        self.incr("coalesced", type=etype)

    def dead_letter(self, reason: str) -> None:
        self.incr("dead_letter", reason=reason)

    def namespace_violation(self, origin: str, etype: str) -> None:
        self.incr("namespace_violation", origin=origin, type=etype)

    def subscription(self, gen: int) -> None:
        self.incr("subscription", gen=gen)

    def offloaded(self, session_id: str) -> None:
        self.incr("offloaded", session_id=session_id)

    # ── reporting ─────────────────────────────────────────────────────────
    def get(self, name: str) -> int:
        return int(self.counters.get(name, 0))

    def snapshot(self) -> dict[str, int]:
        return dict(self.counters)

    def reset(self) -> None:
        self.counters.clear()
        self.recent.clear()


#: Process-wide default collector.  The bus accepts an injected instance so that
#: tests (and later, the tracer) can observe a private collector.
metrics = Metrics()
