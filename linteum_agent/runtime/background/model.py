"""In-process background task model (async ReAct design v0.1 §8.2).

A ``BackgroundTask`` is **process-local state**: it is never serialised into
``AgentState``, never re-executed after a restart, and only its lifecycle facts
travel on the bus as ``background.*`` events.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Optional

#: Manager-internal states.  ``terminal`` is the only one that never leaks to
#: the bus; the concrete terminal status is stored in ``terminal["status"]``.
PENDING_APPROVAL = "pending_approval"
QUEUED = "queued"
STARTING = "starting"
RUNNING = "running"
STOPPING = "stopping"
TERMINAL = "terminal"

#: States that occupy a concurrency slot (approval wait included, so a burst of
#: approvals cannot bypass ``max_running_per_session``).
SLOT_STATES = frozenset({PENDING_APPROVAL, STARTING, RUNNING, STOPPING})

#: Every non-terminal state, including tasks waiting for a free slot.
ACTIVE_STATES = SLOT_STATES | {QUEUED}

TERMINAL_STATUSES = frozenset({"completed", "failed", "stopped", "lost"})


@dataclass
class BackgroundTask:
    """One background execution unit owned by the manager."""

    task_id: str
    start_call_id: str
    session_id: str
    name: str
    kind: str                       # "tool" | "subagent"
    args: dict
    trace_id: str
    start_event_id: str
    status: str = PENDING_APPROVAL
    started_at: float = 0.0
    started_seq: int = 0
    start_published: bool = False
    #: True once the start handshake's approval was granted (or was not
    #: needed), so a later promotion must not ask again.
    approved: bool = False
    stop_requested_by: Optional[str] = None
    stop_reason: Optional[str] = None
    terminal: Optional[dict] = None
    #: The original ``background.start.requested`` event (never serialised).
    start_event: object = None
    #: The ``ToolSpec`` digest captured at start (tool targets only).
    spec_digest: Optional[str] = None
    #: ``asyncio.Task`` waiting out ``stop_grace_ms`` (never serialised).
    stop_timer: Optional[asyncio.Task] = field(default=None, repr=False)

    @property
    def is_terminal(self) -> bool:
        return self.status == TERMINAL

    @property
    def terminal_status(self) -> Optional[str]:
        if self.terminal is None:
            return None
        return self.terminal.get("status")


__all__ = [
    "ACTIVE_STATES",
    "BackgroundTask",
    "PENDING_APPROVAL",
    "QUEUED",
    "RUNNING",
    "SLOT_STATES",
    "STARTING",
    "STOPPING",
    "TERMINAL",
    "TERMINAL_STATUSES",
]
