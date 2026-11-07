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

#: Fixed reducer-side retention for terminal background tasks.  This is a
#: **constant**, not host configuration, so replay is identical regardless of
#: the process's current ``agent.background.max_terminal_tasks`` (§7.1 / R7).
BACKGROUND_TERMINAL_RETAIN = 32

#: Task states the background manager may publish.  ``terminal`` is an internal
#: manager state only; the reducer sees one of the concrete terminal statuses.
BACKGROUND_STATUSES = frozenset({
    "queued", "running", "stopping", "completed", "failed", "stopped", "lost",
})
BACKGROUND_TERMINAL_STATUSES = frozenset({
    "completed", "failed", "stopped", "lost",
})


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
    #: ``call_id`` subset of ``pending`` routed to ``subagent.*`` instead of
    #: ``tool.call.*`` (subagent design §6.3).  It is only a routing marker; the
    #: convergence barrier still uses ``pending``.
    pending_subagents: set = field(default_factory=set)
    #: Terminal ``call_id`` values whose child usage was already added to the
    #: parent budget.  Monotone: guarantees at-most-once accounting on replay or
    #: duplicate terminals (subagent design §6.4 / S13).
    subagent_usage_accounted: set = field(default_factory=set)
    #: ``call_id`` subset of ``pending`` routed to ``background.*`` (the
    #: ``start`` / ``stop`` / ``list_tasks`` calls).  Task ids never enter
    #: ``pending`` (async ReAct design v0.1 T3).
    pending_background: set = field(default_factory=set)
    #: ``task_id -> {task_id,name,kind,status,start_call_id,started_seq,
    #: finished_seq,output_ref,code,error}``.  Only references/summaries are
    #: stored — never the full task output (§7.1 / I11).
    background: dict = field(default_factory=dict)
    #: Background task ids whose subagent usage was added to the parent budget.
    #: Monotone, so duplicate/replayed terminals are counted at most once.
    background_usage_accounted: set = field(default_factory=set)
    #: Messages that arrived while a reasoning step was in flight.  They are
    #: flushed into ``messages`` immediately before the next request so provider
    #: message ordering stays ``assistant(tool_calls) → tool → user`` (D7).
    step_inbox: list = field(default_factory=list)
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
    # ── last applied bus event seq: the WAL replay anchor (checkpoint.seq) ──
    last_event_seq: int = 0
    # ── derived-context compaction records (full text stays in WAL/artifacts) ──
    compactions: list = field(default_factory=list)
    pending_compaction: Optional[dict] = None
    last_context_digest: str = ""

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
        if self.phase == phases.IDLE and self.pending_subagents:  # S3
            problems.append(
                "I2: idle with non-empty pending_subagents "
                f"{sorted(self.pending_subagents)}"
            )
        if not self.pending_subagents <= self.pending:  # S3
            problems.append(
                "S3: pending_subagents is not a subset of pending "
                f"{sorted(self.pending_subagents - self.pending)}"
            )
        if not self.pending_background <= self.pending:  # B1 (§7.1)
            problems.append(
                "B1: pending_background is not a subset of pending "
                f"{sorted(self.pending_background - self.pending)}"
            )
        if self.phase == phases.IDLE:
            if self.pending_background:  # T5
                problems.append(
                    "T5: idle with non-empty pending_background "
                    f"{sorted(self.pending_background)}"
                )
            if self.step_inbox:  # T5
                problems.append(
                    f"T5: idle with non-empty step_inbox ({len(self.step_inbox)})"
                )
        for task_id, meta in self.background.items():
            if not isinstance(meta, dict):
                problems.append(f"B2: background[{task_id!r}] is not a mapping")
                continue
            if meta.get("task_id") != task_id:
                problems.append(
                    f"B2: background[{task_id!r}].task_id is "
                    f"{meta.get('task_id')!r}"
                )
            status = meta.get("status")
            if status not in BACKGROUND_STATUSES:
                problems.append(
                    f"B2: background[{task_id!r}] has status {status!r}"
                )
            if status in BACKGROUND_TERMINAL_STATUSES:
                started = meta.get("started_seq")
                finished = meta.get("finished_seq")
                try:
                    if int(finished) < int(started):
                        problems.append(
                            f"B2: background[{task_id!r}] finished_seq "
                            f"{finished} < started_seq {started}"
                        )
                except (TypeError, ValueError):
                    problems.append(
                        f"B2: background[{task_id!r}] has invalid seq metadata"
                    )
        for index, msg in enumerate(self.messages):
            size = len(json.dumps(msg, ensure_ascii=False).encode("utf-8"))
            if size > INLINE_LIMIT:  # I15
                problems.append(f"I15: message[{index}] is {size}B > {INLINE_LIMIT}B")
        for index, msg in enumerate(self.step_inbox):
            size = len(json.dumps(msg, ensure_ascii=False).encode("utf-8"))
            if size > INLINE_LIMIT:  # I15 / T5
                problems.append(
                    f"I15: step_inbox[{index}] is {size}B > {INLINE_LIMIT}B"
                )
        last_to = 0
        for index, record in enumerate(self.compactions):
            try:
                start = int(record["from_index"])
                end = int(record["to_index"])
            except (KeyError, TypeError, ValueError):
                problems.append(f"COMPACTION: record[{index}] is malformed")
                continue
            if start != last_to:
                problems.append(
                    f"COMPACTION: record[{index}] starts at {start}, expected {last_to}"
                )
            if end <= start or end > len(self.messages):
                problems.append(
                    f"COMPACTION: record[{index}] range [{start},{end}) is invalid"
                )
            last_to = end
        if self.pending_compaction is not None and self.phase != phases.THINKING:
            problems.append(
                f"COMPACTION: pending compaction while phase={self.phase!r}"
            )
        return problems

    def assert_invariants(self) -> None:
        problems = self.invariant_violations()
        if problems:
            raise AssertionError("; ".join(problems))

    # -- accounting ---------------------------------------------------------
    def observe(self, ev: Event) -> None:
        """Record the event clock and replay anchor (reducer-safe; no clock)."""
        self.last_ts = ev.ts
        self.last_event_seq = max(self.last_event_seq, int(ev.seq or 0))

    def add_usage(self, *, input_tokens: int = 0, output_tokens: int = 0, usd: float = 0.0) -> None:
        self.tokens_used += int(input_tokens) + int(output_tokens)
        self.usd_used += float(usd)

    # -- background helpers -------------------------------------------------
    def background_running_ids(self) -> list:
        """Outstanding task ids (queued/running/stopping), in sorted order."""
        return sorted(
            task_id for task_id, meta in self.background.items()
            if meta.get("status") in ("queued", "running", "stopping")
        )

    def prune_background(self, *, keep_task_id: Optional[str] = None) -> None:
        """Keep at most :data:`BACKGROUND_TERMINAL_RETAIN` terminal tasks.

        Running/stopping tasks are never pruned.  ``keep_task_id`` exempts the
        task this reducer step is processing so its notification can still read
        the metadata (§7.5).
        """
        terminal = [
            (int(meta.get("finished_seq") or 0), task_id)
            for task_id, meta in self.background.items()
            if meta.get("status") in BACKGROUND_TERMINAL_STATUSES
            and task_id != keep_task_id
        ]
        overflow = len(terminal) - BACKGROUND_TERMINAL_RETAIN
        if overflow <= 0:
            return
        for _, task_id in sorted(terminal)[:overflow]:
            self.background.pop(task_id, None)

    # -- (de)serialisation --------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "trace_id": self.trace_id,
            "phase": self.phase,
            "messages": self.messages,
            "pending": sorted(self.pending),
            "pending_subagents": sorted(self.pending_subagents),
            "subagent_usage_accounted": sorted(self.subagent_usage_accounted),
            "pending_background": sorted(self.pending_background),
            "background": {task_id: dict(meta) for task_id, meta in self.background.items()},
            "background_usage_accounted": sorted(self.background_usage_accounted),
            "step_inbox": [dict(message) for message in self.step_inbox],
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
            "last_event_seq": self.last_event_seq,
            "compactions": [dict(record) for record in self.compactions],
            "pending_compaction": dict(self.pending_compaction)
            if self.pending_compaction is not None else None,
            "last_context_digest": self.last_context_digest,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "AgentState":
        return cls(
            session_id=data["session_id"],
            trace_id=data["trace_id"],
            phase=data.get("phase", phases.IDLE),
            messages=list(data.get("messages", [])),
            pending=set(data.get("pending", [])),
            pending_subagents=set(data.get("pending_subagents", [])),
            subagent_usage_accounted=set(data.get("subagent_usage_accounted", [])),
            pending_background=set(data.get("pending_background", [])),
            background={
                str(task_id): dict(meta)
                for task_id, meta in (data.get("background") or {}).items()
            },
            background_usage_accounted=set(
                data.get("background_usage_accounted", [])
            ),
            step_inbox=[dict(message) for message in data.get("step_inbox", [])],
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
            last_event_seq=int(data.get("last_event_seq", 0)),
            compactions=list(data.get("compactions", [])),
            pending_compaction=dict(data["pending_compaction"])
            if data.get("pending_compaction") is not None else None,
            last_context_digest=data.get("last_context_digest", ""),
        )

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True)
