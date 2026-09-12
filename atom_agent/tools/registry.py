from __future__ import annotations

import inspect
import threading
from collections.abc import Iterable
from typing import Any

from .core import RuntimeContext, Tool, ToolResult
from .validation import validate_arguments


class ToolRegistry:
    """负责 Tool 对象的注册、导出、校验、授权和执行。"""

    def __init__(self, tools: Iterable[Tool] | None = None, *, context: RuntimeContext | None = None):
        self._tools: dict[str, Tool] = {}
        self._lock = threading.RLock()
        self.context = context or RuntimeContext.cwd()
        for tool in tools or []:
            self.register(tool)

    def register(self, tool: Tool) -> None:
        if not isinstance(tool, Tool):
            raise TypeError("registry.register() 需要 Tool 实例")
        if not tool.name:
            raise ValueError("tool name cannot be empty")
        with self._lock:
            if tool.name in self._tools:
                raise ValueError(f"duplicate tool name: {tool.name}")
            self._tools[tool.name] = tool

    def all(self) -> list[Tool]:
        with self._lock:
            return list(self._tools.values())

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return [tool.name for tool in self.all()]

    def to_llm_tools(self) -> list[dict[str, Any]]:
        return [{"type": "function", "function": {"name": t.name, "description": t.description,
                 "parameters": t.parameters}} for t in self.all() if t.enabled]

    def definitions(self) -> list[Any]:
        try:
            from ..messages.agent_message import ToolDefinition
        except ImportError:
            from messages.agent_message import ToolDefinition
        return [ToolDefinition(t.name, t.description, t.parameters) for t in self.all() if t.enabled]

    def set_enabled(self, name: str, enabled: bool) -> Tool:
        tool = self._require(name)
        tool.enabled = bool(enabled)
        return tool

    def is_enabled(self, name: str) -> bool:
        return self._require(name).enabled

    def _require(self, name: str) -> Tool:
        tool = self.get(name)
        if tool is None:
            raise KeyError(f"unknown tool: {name}")
        return tool

    def _prepare(self, name: str, arguments: dict[str, Any]) -> Tool:
        tool = self._require(name)
        if not tool.enabled:
            raise PermissionError(f"tool is disabled: {name}")
        validate_arguments(tool.parameters, arguments)
        if tool.requires_approval and (self.context.approve is None or not self.context.approve(name, arguments)):
            raise PermissionError(f"tool call not approved: {name}")
        return tool

    def execute_sync(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        if not isinstance(arguments, dict):
            raise ValueError("tool arguments must be an object")
        result = self._prepare(name, arguments).execute(arguments, self.context)
        if inspect.isawaitable(result):
            raise RuntimeError("同步执行不能调用异步工具")
        return result if isinstance(result, ToolResult) else ToolResult(str(result), details={"value": result})

    async def execute(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        if not isinstance(arguments, dict):
            raise ValueError("tool arguments must be an object")
        result = self._prepare(name, arguments).execute(arguments, self.context)
        if inspect.isawaitable(result):
            result = await result
        return result if isinstance(result, ToolResult) else ToolResult(str(result), details={"value": result})

    def tool_info(self, *, include_disabled: bool = True) -> list[dict[str, Any]]:
        return [{"id": t.name, "name": t.name, "description": t.description, "parameters": t.parameters,
                 "enabled": t.enabled, "requires_approval": t.requires_approval}
                for t in self.all() if include_disabled or t.enabled]


__all__ = ["ToolRegistry", "Tool"]