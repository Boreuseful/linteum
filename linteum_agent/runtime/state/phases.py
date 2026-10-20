"""Agent phase state machine (§17).

The phase machine is declared *statically* and enforced on every transition:
an illegal move raises rather than silently corrupting the state, which is how
the design surfaces race-condition bugs early instead of letting them replay
into divergence.

```
idle ──user.message──▶ thinking ──llm.completed(no tools)──▶ idle
                         │  └─llm.completed(with tools)──▶ acting ─▶ awaiting_tools
                         │                                        │  ├─tool.call.*▶ acting
                         │                                        │  ├─pending=∅──▶ thinking
                         │                                        │  └─approval.required▶ awaiting_approval
                         └─user.interrupt──▶ idle                ...
```
"""

from __future__ import annotations

#: Canonical phase names.
IDLE = "idle"
THINKING = "thinking"
ACTING = "acting"
AWAITING_TOOLS = "awaiting_tools"
AWAITING_APPROVAL = "awaiting_approval"
PAUSED = "paused"

PHASES: tuple = (IDLE, THINKING, ACTING, AWAITING_TOOLS, AWAITING_APPROVAL, PAUSED)

#: Legal transitions (target-state sets), derived from the §17 state diagram.
TRANSITIONS: dict[str, frozenset] = {
    IDLE: frozenset({THINKING}),
    THINKING: frozenset({IDLE, ACTING, PAUSED}),
    ACTING: frozenset({AWAITING_TOOLS, PAUSED, IDLE}),
    AWAITING_TOOLS: frozenset({ACTING, THINKING, AWAITING_APPROVAL, IDLE}),
    AWAITING_APPROVAL: frozenset({THINKING, ACTING, PAUSED, IDLE}),
    PAUSED: frozenset({THINKING, ACTING}),
}


class IllegalTransition(RuntimeError):
    """Raised when the reducer attempts a phase change not in the table.

    Subclasses ``RuntimeError`` so hosts can abort a session by type without
    matching on the message text.
    """

    def __init__(self, old: str, new: str) -> None:
        super().__init__(f"illegal phase transition: {old!r} -> {new!r}")
        self.old = old
        self.new = new


def can_transition(old: str, new: str) -> bool:
    """True if ``old -> new`` is legal (a no-op transition to the same phase is legal)."""
    if old == new:
        return True
    return new in TRANSITIONS.get(old, frozenset())


def assert_transition(old: str, new: str) -> None:
    """Raise :class:`IllegalTransition` if the move is not in the table."""
    if not can_transition(old, new):
        raise IllegalTransition(old, new)
