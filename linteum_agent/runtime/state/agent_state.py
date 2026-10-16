"""AgentState + Budget: the pure, replayable state of one session (§17, §20.1).

``AgentState`` holds **only final results** — never ``delta`` streams (I4).  Every
field is JSON-serialisable so a checkpoint is just ``json.dumps(state.to_dict())``
and recovery is ``reduce`` replayed over the WAL (§47).

Two invariants are checked here so they can be asserted in tests and at
checkpoint boundaries:

* **I2** — ``phase == "idle"`` implies ``pending`` is empty.
* **I15** — no single ``messages`` entry exceeds ``INLINE_LIMIT`` (oversized
  payloads must already have been externalised to ``art:`` references).

``Budget`` deliberately contains **no clock call**: elapsed wall-time is derived
from ``state.last_ts`` (set from ``ev.ts``), keeping the reducer free of
``time.time()`` so it stays replayable (§18.1).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Optional

from ...bus.event import INLINE_LIMIT, Event
from . import phases


@dataclass
class Budget:
    """Session budget limits (0 = unlimited) plus usage counters.

    Limits cover token spend (``tokens``), cost (``usd``), wall-clock
    (``wall_ms``) and loop count (``rounds``).  ``rounds`` caps the number of
    LLM requests the agent may issue: because ``state.round`` is the index of the
    upcoming request, ``round >= rounds`` pauses the session once ``rounds``
    requests have already gone out.

    ``started_ms`` is a wall-clock start supplied by the host; the reducer never
    reads the clock — :meth:`check` uses ``state.last_ts`` (from ``ev.ts``).
    """

    tokens: int = 0
    usd: float = 0.0
    wall_ms: int = 0
    started_ms: float = 0.0
    rounds: int = 0

    def check(self, state: "AgentState") -> Optional[str]:
        """Return a human-readable overrun reason, or ``None`` if within budget."""
        if self.tokens and state.tokens_used > self.tokens:
            return f"tokens {state.tokens_used} > {self.tokens}"
        if self.usd and state.usd_used > self.usd:
            return f"usd {state.usd_used:.6f} > {self.usd:.6f}"
        if self.wall_ms and self.started_ms and state.last_ts:
            elapsed_ms = (state.last_ts - self.started_ms) * 1000.0
            if elapsed_ms > self.wall_ms:
                return f"wall {elapsed_ms:.0f}ms > {self.wall_ms}ms"
        if self.rounds and state.round >= self.rounds:
            return f"rounds {state.round} >= {self.rounds}"
        return None

    def to_dict(self) -> dict:
        return {
            "tokens": self.tokens,
            "usd": self.usd,
            "wall_ms": self.wall_ms,
            "started_ms": self.started_ms,
            "rounds": self.rounds,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Budget":
        return cls(
            tokens=int(data.get("tokens", 0)),
            usd=float(data.get("usd", 0.0)),
            wall_ms=int(data.get("wall_ms", 0)),
            started_ms=float(data.get("started_ms", 0.0)),
            rounds=int(data.get("rounds", 0)),
        )


@dataclass
class AgentState:
    """The replayable state of a single agent session (§17)."""

    session_id: str
    trace_id: str
    phase: str = phases.IDLE
    messages: list = field(default_factory=list)      # final results only (I4)
    pending: set = field(default_factory=set)          # unfinished call_ids
    cancelled: set = field(default_factory=set)        # call_ids voided by an interrupt (§16.3)
    interrupt_pending: bool = False                    # awaiting an interrupted LLM completion to tag
    plan: list = field(default_factory=list)
    round: int = 0
    budget: Budget = field(default_factory=Budget)
    seq: int = 0
    version: int = 0
    config_digest: str = ""
    plugin_versions: dict = field(default_factory=dict)
    bound_model: Optional[str] = None                  # session-pinned provider/model (I25)
    inflight_request_id: Optional[str] = None          # event id of the live ``llm.request`` (correlation)
    # ── budget accounting (updated from llm.completed usage) ──
    tokens_used: int = 0
    usd_used: float = 0.0
    # ── last observed event time (drives budget wall check; no clock in reducer) ──
    last_ts: float = 0.0

    # -- transitions --------------------------------------------------------
    def set_phase(self, new: str) -> None:
        """Move to ``new``, enforcing the static transition table (§17)."""
        phases.assert_transition(self.phase, new)
        self.phase = new

    # -- invariant checks ---------------------------------------------------
    def invariant_violations(self) -> list:
        """Return the list of broken invariants (empty == healthy)."""
        problems: list = []
        if self.phase == phases.IDLE and self.pending:  # I2
            problems.append(f"I2: idle with non-empty pending {sorted(self.pending)}")
        for index, msg in enumerate(self.messages):
            size = len(json.dumps(msg, ensure_ascii=False).encode("utf-8"))
            if size > INLINE_LIMIT:  # I15
                problems.append(f"I15: message[{index}] is {size}B > {INLINE_LIMIT}B")
        return problems

    def assert_invariants(self) -> None:
        problems = self.invariant_violations()
        if problems:
            raise AssertionError("; ".join(problems))

    # -- accounting ---------------------------------------------------------
    def observe(self, ev: Event) -> None:
        """Record the event clock (reducer-safe; no ``time.time()``)."""
        self.last_ts = ev.ts

    def add_usage(self, *, input_tokens: int = 0, output_tokens: int = 0, usd: float = 0.0) -> None:
        self.tokens_used += int(input_tokens) + int(output_tokens)
        self.usd_used += float(usd)

    # -- (de)serialisation --------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "trace_id": self.trace_id,
            "phase": self.phase,
            "messages": self.messages,
            "pending": sorted(self.pending),
            "cancelled": sorted(self.cancelled),
            "interrupt_pending": self.interrupt_pending,
            "plan": self.plan,
            "round": self.round,
            "budget": self.budget.to_dict(),
            "seq": self.seq,
            "version": self.version,
            "config_digest": self.config_digest,
            "plugin_versions": self.plugin_versions,
            "bound_model": self.bound_model,
            "inflight_request_id": self.inflight_request_id,
            "tokens_used": self.tokens_used,
            "usd_used": self.usd_used,
            "last_ts": self.last_ts,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "AgentState":
        return cls(
            session_id=data["session_id"],
            trace_id=data["trace_id"],
            phase=data.get("phase", phases.IDLE),
            messages=list(data.get("messages", [])),
            pending=set(data.get("pending", [])),
            cancelled=set(data.get("cancelled", [])),
            interrupt_pending=bool(data.get("interrupt_pending", False)),
            plan=list(data.get("plan", [])),
            round=int(data.get("round", 0)),
            budget=Budget.from_dict(data.get("budget", {})),
            seq=int(data.get("seq", 0)),
            version=int(data.get("version", 0)),
            config_digest=data.get("config_digest", ""),
            plugin_versions=dict(data.get("plugin_versions", {})),
            bound_model=data.get("bound_model"),
            inflight_request_id=data.get("inflight_request_id"),
            tokens_used=int(data.get("tokens_used", 0)),
            usd_used=float(data.get("usd_used", 0.0)),
            last_ts=float(data.get("last_ts", 0.0)),
        )

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True)
