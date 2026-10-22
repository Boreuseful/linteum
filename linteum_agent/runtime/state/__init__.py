"""Agent state layer (design doc §17–§20.1).

* :mod:`linteum_agent.runtime.state.phases` — the static phase machine.
* :mod:`linteum_agent.runtime.state.agent_state` — :class:`AgentState` and :class:`Budget`.
* :mod:`linteum_agent.runtime.state.checkpoint` — snapshotting / replay start point.
"""

from . import phases
from .agent_state import AgentState, Budget
from .checkpoint import Checkpoint, Checkpointer, InMemoryCheckpointer
from .phases import IllegalTransition, assert_transition, can_transition

__all__ = [
    "phases",
    "AgentState",
    "Budget",
    "Checkpoint",
    "Checkpointer",
    "InMemoryCheckpointer",
    "IllegalTransition",
    "assert_transition",
    "can_transition",
]
