"""Value objects shared by the subagent catalog and orchestrator (§7.3).

They are **host-side** objects: they hold a :class:`Budget` and effective tool
lists, so they never travel on the bus.  Only plain JSON derived from them
(``subagent.started`` / ``completed`` payloads) is published.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

from ..state.agent_state import Budget


@dataclass(frozen=True, slots=True)
class SubagentPolicy:
    """The effective, host-approved execution boundary for one child."""

    tools: tuple[str, ...]
    model_profile: str
    read_only: bool
    timeout_ms: int
    timeout_clamped: bool
    approval_policy: str
    budget: Budget

    def started_payload(self, *, call_id: str, child_session_id: str,
                        parent_session_id: str) -> dict:
        """The ``subagent.started`` payload (design §5.3)."""
        return {
            "call_id": call_id,
            "child_session_id": child_session_id,
            "parent_session_id": parent_session_id,
            "tools": list(self.tools),
            "model_profile": self.model_profile,
            "read_only": self.read_only,
            "approval_policy": self.approval_policy,
            "timeout_ms": self.timeout_ms,
            "timeout_clamped": self.timeout_clamped,
            "status": "running",
        }


@dataclass(frozen=True, slots=True)
class SubagentTask:
    """One validated spawn request held by the orchestrator."""

    call_id: str
    parent_session_id: str
    spawn_event_id: str
    trace_id: str
    title: str
    task: str
    instructions: str
    context: str = ""
    environment: str = ""
    expected_output: str = ""
    policy: Optional[SubagentPolicy] = None

    @property
    def prompt_bytes(self) -> int:
        return sum(
            len(value.encode("utf-8"))
            for value in (
                self.task, self.instructions, self.context,
                self.environment, self.expected_output,
            )
        )


@dataclass(frozen=True, slots=True)
class SubagentResult:
    """The execution metadata returned with a successful terminal."""

    child_session_id: Optional[str]
    status: str
    rounds: int
    usage: Mapping[str, Any] = field(default_factory=dict)
    workspace: Optional[str] = None
    duration_ms: int = 0

    def to_dict(self) -> dict:
        return {
            "child_session_id": self.child_session_id,
            "status": self.status,
            "rounds": self.rounds,
            "usage": dict(self.usage),
            "workspace": self.workspace,
            "duration_ms": self.duration_ms,
        }


__all__ = ["SubagentPolicy", "SubagentResult", "SubagentTask"]
