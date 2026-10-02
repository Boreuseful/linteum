"""The public Tool contracts used by the agent tool runtime."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

try:
    from ..context.session import TOOL_CONFIG_KEY, ApproveFn, Session
except ImportError:  # 支持直接将 /workspace/atom_agent 加入 sys.path
    from context.session import TOOL_CONFIG_KEY, ApproveFn, Session

# 工具运行上下文已经合并进 ``context.Session``：
# - ``Session.effective_workspace``：工具工作区；
# - ``Session.metadata``：会话元数据；``Session.tools``：工具配置；
# - ``Session.approve`` / ``Session.cancelled`` / ``log()`` / ``is_cancelled()``：
#   执行控制与输出通道。
#
# 旧的 ``RuntimeContext`` 名字仍然可用，但它只是 ``Session`` 的兼容别名。
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
