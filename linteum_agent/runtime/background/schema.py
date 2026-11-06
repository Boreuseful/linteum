"""Provider schemas for the reserved ``start`` / ``stop`` / ``list_tasks`` tools.

The three names are **not** ``ToolSpec`` entries and never reach
``ToolRuntime``: the Agent routes them to ``background.*`` intent events.  The
host injects these schemas only when ``agent.background.enabled`` is true and at
least one target is eligible (async ReAct design v0.1 §5.1 / §5.5).
"""

from __future__ import annotations

from typing import Sequence

#: The stable reserved names (host-owned; plugins may not register them).
START_TOOL = "start"
STOP_TOOL = "stop"
LIST_TOOL = "list_tasks"
BACKGROUND_TOOL_NAMES = frozenset({START_TOOL, STOP_TOOL, LIST_TOOL})


def background_tool_schemas(targets: Sequence[str]) -> list[dict]:
    """Build the three function schemas with ``targets`` as ``start.tool`` enum.

    An empty target list yields ``[]`` — the caller must not inject a ``start``
    schema whose ``enum`` is empty (§5.5).
    """
    names = sorted(set(targets))
    if not names:
        return []
    return [
        {
            "type": "function",
            "function": {
                "name": START_TOOL,
                "description": (
                    "Start a long-running tool or subagent in the background. "
                    "Returns immediately with a task_id. The final result "
                    "arrives later as a background notification in this "
                    "conversation; do not poll for it. Use stop(task_id) to "
                    "cancel. Only host-approved background-capable targets are "
                    "accepted."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "tool": {
                            "type": "string",
                            "description": (
                                "Name of the background-capable tool, or the "
                                "configured subagent tool name."
                            ),
                            "enum": names,
                        },
                        "args": {
                            "type": "object",
                            "description": (
                                "Arguments for the target tool, validated "
                                "against its own schema."
                            ),
                        },
                        "reason": {
                            "type": "string",
                            "description": (
                                "Optional short note explaining why this runs "
                                "in the background."
                            ),
                        },
                    },
                    "required": ["tool", "args"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": STOP_TOOL,
                "description": (
                    "Request cancellation of a running background task. The "
                    "task will stop asynchronously; a background notification "
                    "confirms it."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "task_id": {
                            "type": "string",
                            "description": "The task_id returned by start().",
                        },
                        "reason": {
                            "type": "string",
                            "description": "Optional reason for stopping.",
                        },
                    },
                    "required": ["task_id"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": LIST_TOOL,
                "description": (
                    "List background tasks for this session that are running or "
                    "recently finished. Use it after context compaction to "
                    "recover task ids."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
            },
        },
    ]


__all__ = [
    "BACKGROUND_TOOL_NAMES",
    "LIST_TOOL",
    "START_TOOL",
    "STOP_TOOL",
    "background_tool_schemas",
]
