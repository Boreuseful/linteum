"""Backpressure classification: BLOCK / COALESCE / SHED (§13.1).

There are no unbounded queues in the design (Checklist #10).  Every event class
is assigned a policy *by type*, and the policies have sharp semantics:

* **BLOCK** — the producer waits.  Used for anything whose loss corrupts state
  (``user.*``, ``approval.*``, and ``tool.call.requested`` because dropping it
  would leave a tool call hanging forever — I3).
* **COALESCE** — high-frequency ``*.delta`` streams may be merged within a
  50 ms / 4 KB window when the bus is more than ``high`` full.
* **SHED** — background / backfill / eval traffic (``priority == 9``) is simply
  dropped once pressure crosses ``critical``.

The per-origin WFQ queues (`fair_scheduler`) own the *action*; this module owns
the *decision* so that the policy table is a single, reviewable place.
"""

from __future__ import annotations

from enum import Enum

from .event import Event

#: Default watermarks (fraction of the per-origin queue cap).
HIGH = 0.7
CRITICAL = 0.9

#: Priority at/above which traffic is background and therefore shed-able.
BACKGROUND_PRIORITY = 9


class BackpressurePolicy(Enum):
    BLOCK = "block"
    COALESCE = "coalesce"
    SHED = "shed"


#: Exact types whose loss is unrecoverable -> never shed, always block.
_BLOCK_EXACT: frozenset = frozenset(
    {"tool.call.requested", "tool.cancel.requested", "user.interrupt"}
)
#: Prefixes whose loss is unrecoverable -> never shed, always block.
_BLOCK_PREFIX: tuple = ("user.", "approval.")


def policy_for(ev: Event) -> BackpressurePolicy:
    """Classify an event into one of the three backpressure policies."""
    etype = ev.type
    if etype.endswith(".delta"):
        return BackpressurePolicy.COALESCE
    if etype in _BLOCK_EXACT or any(etype.startswith(p) for p in _BLOCK_PREFIX):
        return BackpressurePolicy.BLOCK
    if ev.priority >= BACKGROUND_PRIORITY:
        return BackpressurePolicy.SHED
    # Default for ordinary core traffic: correctness first, so block.
    return BackpressurePolicy.BLOCK


class Backpressure:
    """Watermark-based shedding/shed decision tree.

    ``pressure`` is a 0..1 fraction supplied by the caller (the fair scheduler
    reports the fullest origin's occupancy).  Keeping this stateless makes it
    trivially testable and reusable by the scheduler stage.
    """

    def __init__(self, high: float = HIGH, critical: float = CRITICAL) -> None:
        if not 0.0 < high <= critical <= 1.0:
            raise ValueError("require 0 < high <= critical <= 1")
        self.high = high
        self.critical = critical

    def should_shed(self, ev: Event, pressure: float) -> bool:
        """Shed only strictly-background traffic, and only past ``critical``."""
        if pressure < self.critical:
            return False
        return policy_for(ev) is BackpressurePolicy.SHED

    def should_coalesce(self, ev: Event, pressure: float) -> bool:
        """Whether a delta should be merged rather than delivered as-is."""
        if pressure < self.high:
            return False
        return policy_for(ev) is BackpressurePolicy.COALESCE
