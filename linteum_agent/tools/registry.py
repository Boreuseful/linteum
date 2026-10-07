from __future__ import annotations

import inspect
import threading
from collections.abc import Iterable
from typing import Any

from .core import ApproveFn, Session, Tool, ToolResult
from .validation import validate_arguments


class ToolRegistry:
    """Responsible for registration, export, validation, approval, and execution of Tool objects.

    All configuration needed for tool execution comes from ``session`` (i.e. ``context.Session``):

    - ``session.effective_workspace``: the tool workspace (built-in Tools read it for path constraints);
    - ``session.tools``: session-level tool config (``registered`` / ``enabled`` / ``disabled`` / ``options``);
    - ``session.tools["registered"]``: tools registered for this session (written on ``register()``,
      readable via ``session.registered_tools()``);
    - ``session.approve``: the approval callback for ``requires_approval`` tools (can also be passed
      directly via ``approve=``, equivalent to setting ``session.approve``);
    - ``session.cancelled``: the cancellation check before execution;
    - ``session.metadata`` / ``session.log()``: session info and the tool output channel.

    The ``context=`` parameter and the ``context`` property are compatibility aliases for ``session``.
    """

    def __init__(
        self,
        tools: Iterable[Tool] | None = None,
        *,
        session: Session | None = None,
        context: Session | None = None,
        approve: ApproveFn | None = None,
    ):
        self._tools: dict[str, Tool] = {}
        self._lock = threading.RLock()
        self.session: Session = session or context or Session.cwd()
        if approve is not None:
            # requires_approval tools call session.approve before execution; this provides a direct injection point.
            self.session.approve = approve
        for tool in tools or []:
            self.register(tool)

    # ------------------------------------------------------------------
    # Session binding
    # ------------------------------------------------------------------

    @property
    def context(self) -> Session:
        """Compatibility with the old naming: the tool runtime context is just ``self.session``."""
        return self.session

    @context.setter
    def context(self, value: Session) -> None:
        self.session = value

    @property
    def workspace(self) -> Any:
        """The tool workspace (reads the session workspace in real time)."""
        return self.session.effective_workspace

    def bind_session(self, session: Session) -> Session:
        """Switch the session used by the tool registry, record the registered tools into that session, and return it."""
        self.session = session
        session.register_tools(self.all())
        return session

    # ------------------------------------------------------------------
    # Registration and lookup
    # ------------------------------------------------------------------

    def register(self, tool: Tool) -> None:
        if not isinstance(tool, Tool):
            raise TypeError("registry.register() requires a Tool instance")
        if not tool.name:
            raise ValueError("tool name cannot be empty")
        with self._lock:
            if tool.name in self._tools:
                raise ValueError(f"duplicate tool name: {tool.name}")
            self._tools[tool.name] = tool
        # Record the tools registered (available) for this session, for session.registered_tools() to read.
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
        """Toggle the Tool's own ``enabled`` switch (session config still takes precedence)."""
        tool = self._require(name)
        tool.enabled = bool(enabled)
        return tool

    def is_enabled(self, name: str) -> bool:
        return self.is_tool_enabled(name)

    def is_tool_enabled(self, name: str) -> bool:
        """Determine availability by combining the Tool's own switch with the session tool config."""
        tool = self._require(name)
        return tool.enabled and self.session.tool_enabled(name)

    def tool_options(self, name: str | None = None) -> dict[str, Any]:
        """Return the option config for this tool (or the global one) in the session."""
        return self.session.tool_options(name)

    def _require(self, name: str) -> Tool:
        tool = self.get(name)
        if tool is None:
            raise KeyError(f"unknown tool: {name}")
        return tool

    # ------------------------------------------------------------------
    # Execution
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
            raise RuntimeError("synchronous execution cannot call an asynchronous tool")
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


__all__ = ["ToolRegistry", "Tool", "Session", "ApproveFn"]
