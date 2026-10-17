"""Checkpointing: the replay start point (§18.3).

Snapshots are taken at well-defined boundaries and the **most recent 3** are kept
so a single corrupt snapshot cannot strand a session (§18.3).  Checkpointing is a
host-side concern (it reads the wall clock and touches the filesystem in a real
deployment); the reducer only calls :meth:`Checkpointer.maybe_snapshot` and never
lets the result affect state.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional, Protocol, runtime_checkable

from . import phases
from .agent_state import AgentState

#: Snapshot every N events as a fallback between round boundaries (§18.3).
DEFAULT_EVERY_N = 500
#: Keep the most recent N snapshots.
DEFAULT_KEEP = 3


def should_snapshot(state: AgentState, *, every_n_events: int, force: bool = False) -> bool:
    """Snapshot trigger shared by the in-memory and persistent checkpointers."""
    if force:
        return True
    if state.phase == phases.PAUSED:            # must snapshot before pausing
        return True
    if not state.pending:                        # round boundary (primary)
        return True
    # fallback: every N events (guard version 0 so a fresh state is not a trigger)
    return state.version > 0 and state.version % max(1, every_n_events) == 0


@dataclass(frozen=True)
class Checkpoint:
    """An immutable snapshot of ``AgentState`` at ``seq``."""

    session_id: str
    seq: int
    version: int
    phase: str
    state: dict
    created: float


@runtime_checkable
class Checkpointer(Protocol):
    def maybe_snapshot(self, state: AgentState, *, force: bool = False) -> Optional[Checkpoint]: ...

    def latest(self, session_id: str) -> Optional[Checkpoint]: ...

    def all(self, session_id: str) -> list: ...


class InMemoryCheckpointer:
    """Keeps the last ``keep`` snapshots per session (default 3)."""

    def __init__(self, *, keep: int = DEFAULT_KEEP, every_n_events: int = DEFAULT_EVERY_N) -> None:
        if keep < 1:
            raise ValueError("keep must be >= 1")
        self.keep = keep
        self.every_n_events = max(1, every_n_events)
        self._by_session: dict[str, list] = {}

    # -- triggers -----------------------------------------------------------
    def _should_snapshot(self, state: AgentState, force: bool) -> bool:
        return should_snapshot(
            state, every_n_events=self.every_n_events, force=force
        )

    def maybe_snapshot(self, state: AgentState, *, force: bool = False) -> Optional[Checkpoint]:
        if not self._should_snapshot(state, force):
            return None
        cp = Checkpoint(
            session_id=state.session_id,
            seq=state.last_event_seq,
            version=state.version,
            phase=state.phase,
            state=state.to_dict(),
            created=time.time(),
        )
        bucket = self._by_session.setdefault(state.session_id, [])
        bucket.append(cp)
        if len(bucket) > self.keep:
            del bucket[: len(bucket) - self.keep]
        return cp

    # -- reads --------------------------------------------------------------
    def latest(self, session_id: str) -> Optional[Checkpoint]:
        bucket = self._by_session.get(session_id)
        return bucket[-1] if bucket else None

    def all(self, session_id: str) -> list:
        return list(self._by_session.get(session_id, ()))

    def delete_session(self, session_id: str) -> None:
        self._by_session.pop(session_id, None)

    def restore(self, session_id: str) -> Optional[AgentState]:
        cp = self.latest(session_id)
        return AgentState.from_dict(cp.state) if cp else None
