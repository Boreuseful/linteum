"""Background tasks / asynchronous ReAct (async ReAct design v0.1).

The package is host-facing glue: ``bus`` does not know it, tools/subagents may
not import it, and ``Agent`` only reduces the resulting ``background.*`` facts.
"""

from .errors import (
    BackgroundError,
    E_BACKGROUND_ARGS,
    E_BACKGROUND_CANCELLED,
    E_BACKGROUND_DISABLED,
    E_BACKGROUND_LIMIT,
    E_BACKGROUND_LOST,
    E_BACKGROUND_NOT_ALLOWED,
    E_BACKGROUND_NOT_FOUND,
    E_BACKGROUND_NOT_RUNNING,
    E_BACKGROUND_SPAWN,
    E_BACKGROUND_STOPPED,
    E_BACKGROUND_TIMEOUT,
    E_BACKGROUND_UNKNOWN_TOOL,
)
from .manager import BackgroundTaskManager
from .model import (
    ACTIVE_STATES,
    BackgroundTask,
    PENDING_APPROVAL,
    QUEUED,
    RUNNING,
    SLOT_STATES,
    STARTING,
    STOPPING,
    TERMINAL,
    TERMINAL_STATUSES,
)
from .schema import (
    BACKGROUND_TOOL_NAMES,
    LIST_TOOL,
    START_TOOL,
    STOP_TOOL,
    background_tool_schemas,
)

__all__ = [
    "ACTIVE_STATES",
    "BACKGROUND_TOOL_NAMES",
    "BackgroundError",
    "BackgroundTask",
    "BackgroundTaskManager",
    "E_BACKGROUND_ARGS",
    "E_BACKGROUND_CANCELLED",
    "E_BACKGROUND_DISABLED",
    "E_BACKGROUND_LIMIT",
    "E_BACKGROUND_LOST",
    "E_BACKGROUND_NOT_ALLOWED",
    "E_BACKGROUND_NOT_FOUND",
    "E_BACKGROUND_NOT_RUNNING",
    "E_BACKGROUND_SPAWN",
    "E_BACKGROUND_STOPPED",
    "E_BACKGROUND_TIMEOUT",
    "E_BACKGROUND_UNKNOWN_TOOL",
    "LIST_TOOL",
    "PENDING_APPROVAL",
    "QUEUED",
    "RUNNING",
    "SLOT_STATES",
    "STARTING",
    "STOPPING",
    "START_TOOL",
    "STOP_TOOL",
    "TERMINAL",
    "TERMINAL_STATUSES",
    "background_tool_schemas",
]
