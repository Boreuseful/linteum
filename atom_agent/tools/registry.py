from __future__ import annotations

import inspect
import threading
from collections.abc import Iterable
from typing import Any

from .core import Session, Tool, ToolResult
from .validation import validate_arguments


class ToolRegistry:
    """负责 Tool 对象的注册、导出、校验、授权和执行。

    工具执行所需的配置全部来自 ``session``（即 ``context.Session``）：

    - ``session.effective_workspace``：工具工作区（内置 Tool 读取它做路径约束）；
    - ``session.tools``：会话级工具配置（``registered`` / ``enabled`` / ``disabled`` / ``options``）；
    - ``session.tools["registered"]``：本会话注册的工具（``register()`` 时写入，
      可用 ``session.registered_tools()`` 读取）；
    - ``session.approve``：``requires_approval`` 工具的授权回调；
    - ``session.cancelled``：执行前的取消检查；
    - ``session.metadata`` / ``session.log()``：会话信息与工具输出通道。

    ``context=`` 参数与 ``context`` 属性是 ``session`` 的兼容别名。
    """

    def __init__(
        self,
        tools: Iterable[Tool] | None = None,
        *,
        session: Session | None = None,
        context: Session | None = None,
    ):
        self._tools: dict[str, Tool] = {}
        self._lock = threading.RLock()
        self.session: Session = session or context or Session.cwd()
        for tool in tools or []:
            self.register(tool)

    # ------------------------------------------------------------------
    # Session 绑定
    # ------------------------------------------------------------------

    @property
    def context(self) -> Session:
        """兼容旧命名：工具运行上下文就是 ``self.session``。"""
        return self.session

    @context.setter
    def context(self, value: Session) -> None:
        self.session = value

    @property
    def workspace(self) -> Any:
        """工具工作区（实时读取会话工作区）。"""
        return self.session.effective_workspace

    def bind_session(self, session: Session) -> Session:
        """切换工具注册表使用的会话，并把已注册工具记录到该会话，返回它。"""
        self.session = session
        session.register_tools(self.all())
        return session

    # ------------------------------------------------------------------
    # 注册与查询
    # ------------------------------------------------------------------

    def register(self, tool: Tool) -> None:
        if not isinstance(tool, Tool):
            raise TypeError("registry.register() 需要 Tool 实例")
        if not tool.name:
            raise ValueError("tool name cannot be empty")
        with self._lock:
            if tool.name in self._tools:
                raise ValueError(f"duplicate tool name: {tool.name}")
            self._tools[tool.name] = tool
        # 在会话中登记本会话注册（可用）的工具，供 session.registered_tools() 读取。
        self.session.register_tools(tool)

    def all(self) -> list[Tool]:
        with self._lock:
            return list(self._tools.values())

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return [tool.name for tool in self.all()]

    def to_llm_tools(self) -> list[dict[str, Any]]:
        return [{"type": "function", "function": {"name": t.name, "description": t.description,
                 "parameters": t.parameters}} for t in self.all() if self.is_tool_enabled(t.name)]

    def definitions(self) -> list[Any]:
        try:
            from ..messages.agent_message import ToolDefinition
        except ImportError:
            from messages.agent_message import ToolDefinition
        return [ToolDefinition(t.name, t.description, t.parameters)
                for t in self.all() if self.is_tool_enabled(t.name)]

    def set_enabled(self, name: str, enabled: bool) -> Tool:
        """切换 Tool 自身的 ``enabled`` 开关（会话配置仍然优先）。"""
        tool = self._require(name)
        tool.enabled = bool(enabled)
        return tool

    def is_enabled(self, name: str) -> bool:
        return self.is_tool_enabled(name)

    def is_tool_enabled(self, name: str) -> bool:
        """结合 Tool 自身开关与会话工具配置判断是否可用。"""
        tool = self._require(name)
        return tool.enabled and self.session.tool_enabled(name)

    def tool_options(self, name: str | None = None) -> dict[str, Any]:
        """返回会话中该工具（或全局）的选项配置。"""
        return self.session.tool_options(name)

    def _require(self, name: str) -> Tool:
        tool = self.get(name)
        if tool is None:
            raise KeyError(f"unknown tool: {name}")
        return tool

    # ------------------------------------------------------------------
    # 执行
    # ------------------------------------------------------------------

    def _prepare(self, name: str, arguments: dict[str, Any]) -> Tool:
        tool = self._require(name)
        if not tool.enabled:
            raise PermissionError(f"tool is disabled: {name}")
        if not self.session.tool_enabled(name):
            raise PermissionError(f"tool is disabled by session config: {name}")
        if self.session.is_cancelled():
            raise RuntimeError(f"session cancelled before tool execution: {name}")
        validate_arguments(tool.parameters, arguments)
        if tool.requires_approval and (
            self.session.approve is None or not self.session.approve(name, arguments)
        ):
            raise PermissionError(f"tool call not approved: {name}")
        return tool

    def execute_sync(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        if not isinstance(arguments, dict):
            raise ValueError("tool arguments must be an object")
        result = self._prepare(name, arguments).execute(arguments, self.session)
        if inspect.isawaitable(result):
            raise RuntimeError("同步执行不能调用异步工具")
        return result if isinstance(result, ToolResult) else ToolResult(str(result), details={"value": result})

    async def execute(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        if not isinstance(arguments, dict):
            raise ValueError("tool arguments must be an object")
        result = self._prepare(name, arguments).execute(arguments, self.session)
        if inspect.isawaitable(result):
            result = await result
        return result if isinstance(result, ToolResult) else ToolResult(str(result), details={"value": result})

    def tool_info(self, *, include_disabled: bool = True) -> list[dict[str, Any]]:
        return [{"id": t.name, "name": t.name, "description": t.description, "parameters": t.parameters,
                 "enabled": t.enabled and self.session.tool_enabled(t.name),
                 "requires_approval": t.requires_approval}
                for t in self.all() if include_disabled or self.is_tool_enabled(t.name)]


__all__ = ["ToolRegistry", "Tool", "Session"]
