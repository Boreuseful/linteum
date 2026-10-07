"""The public Tool contracts used by the agent tool runtime."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

try:
    from ..context.session import TOOL_CONFIG_KEY, ApproveFn, Session
except ImportError:  # supports adding /workspace/linteum_agent to sys.path directly
    from context.session import TOOL_CONFIG_KEY, ApproveFn, Session

# The tool runtime context has been merged into ``context.Session``:
# - ``Session.effective_workspace``: the tool workspace;
# - ``Session.metadata``: session metadata; ``Session.tools``: tool config;
# - ``Session.approve`` / ``Session.cancelled`` / ``log()`` / ``is_cancelled()``:
#   execution control and output channels.
#
# The old ``RuntimeContext`` name is still available, but it is only a compatibility alias for
# ``Session``.
RuntimeContext = Session


@dataclass
class ToolResult:
    content: str
    is_error: bool = False
    details: dict[str, Any] | None = None


ExecuteFn = Callable[[dict[str, Any], Session], ToolResult]


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    execute_fn: ExecuteFn
    enabled: bool = True
    requires_approval: bool = False

    def execute(self, arguments: dict[str, Any], context: Session) -> ToolResult:
        return self.execute_fn(arguments, context)


__all__ = ["Session", "RuntimeContext", "Tool", "ToolResult", "ExecuteFn", "ApproveFn", "TOOL_CONFIG_KEY"]
