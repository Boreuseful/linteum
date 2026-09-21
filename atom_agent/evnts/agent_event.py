from __future__ import annotations

import inspect
from typing import (
    Any,
    Awaitable,
    Callable,
    Literal,
    Protocol,
    TypedDict,
    Union,
)

try:
    from ..messages.agent_message import SystemPrompt, UserPrompt, AssistantMessage, ToolResult
except ImportError:  # 支持将 atom_agent 目录直接加入 sys.path
    from messages.agent_message import SystemPrompt, UserPrompt, AssistantMessage, ToolResult

AgentMessage = Union[
    AssistantMessage,
    SystemPrompt,
    UserPrompt
]

class AgentStartEvent(TypedDict):
    type: Literal["agent_start"]


class AgentEndEvent(TypedDict):
    type: Literal["agent_end"]
    messages: list[AgentMessage]


class TurnStartEvent(TypedDict):
    type: Literal["turn_start"]


class TurnEndEvent(TypedDict):
    type: Literal["turn_end"]
    message: AgentMessage
    tool_results: list[ToolResult]


class MessageStartEvent(TypedDict):
    type: Literal["message_start"]
    message: AgentMessage


class MessageUpdateEvent(TypedDict):
    type: Literal["message_update"]
    message: AgentMessage
    assistant_message_event: Any

class MessageEndEvent(TypedDict):
    type: Literal["message_end"]
    message: AgentMessage


class ToolExecutionStartEvent(TypedDict):
    type: Literal["tool_execution_start"]
    tool_call_id: str
    tool_name: str
    args: Any


class ToolExecutionUpdateEvent(TypedDict):
    type: Literal["tool_execution_update"]
    tool_call_id: str
    tool_name: str
    args: Any
    partial_result: Any


class ToolExecutionEndEvent(TypedDict):
    type: Literal["tool_execution_end"]
    tool_call_id: str
    tool_name: str
    result: Any
    is_error: bool


AgentEvent = Union[
    # 第 1 层：Agent 生命周期
    AgentStartEvent,
    AgentEndEvent,

    # 第 2 层：Turn 生命周期
    TurnStartEvent,
    TurnEndEvent,

    # 第 3 层：Message 生命周期
    MessageStartEvent,
    MessageUpdateEvent,
    MessageEndEvent,

    # 第 4 层：Tool Execution 生命周期
    ToolExecutionStartEvent,
    ToolExecutionUpdateEvent,
    ToolExecutionEndEvent,
]

class AgentEventListener:
    """
    全量事件监听器接口。
    """

    async def on_event(self, event: AgentEvent) -> None:
        raise NotImplementedError

EventHandler = Callable[
    [AgentEvent],
    Awaitable[None] | None,
]

class EventBus:
    def __init__(self) -> None:
        self._handlers: dict[str, list[EventHandler]] = {}
        self._listeners: list[AgentEventListener] = []

    def on(
        self,
        event_type: str,
        handler: EventHandler,
    ) -> Callable[[], None]:
        """
        按事件类型监听。

        例如：
            event_bus.on("message_update", handler)
        """
        self._handlers.setdefault(event_type, []).append(handler)

        def unsubscribe() -> None:
            handlers = self._handlers.get(event_type, [])
            if handler in handlers:
                handlers.remove(handler)

        return unsubscribe

    def add_listener(
        self,
        listener: AgentEventListener,
    ) -> Callable[[], None]:
        """
        接收所有 AgentEvent。
        """
        self._listeners.append(listener)

        def unsubscribe() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return unsubscribe

    async def emit(self, event: AgentEvent) -> None:
        event_type = event["type"]

        handlers: list[EventHandler] = []
        handlers.extend(self._handlers.get(event_type, []))
        handlers.extend(listener.on_event for listener in self._listeners)

        for handler in handlers:
            result = handler(event)

            if inspect.isawaitable(result):
                await result

    def emit_sync(self, event: AgentEvent) -> None:
        """同步版本，供 ``AgentEngine.stream`` 和 ``run`` 使用。"""
        event_type = event["type"]
        handlers: list[EventHandler] = [*self._handlers.get(event_type, [])]
        handlers.extend(listener.on_event for listener in self._listeners)
        for handler in handlers:
            result = handler(event)
            if inspect.isawaitable(result):
                try:
                    import asyncio
                    asyncio.get_running_loop()
                except RuntimeError:
                    asyncio.run(result)
                else:
                    # 同步 API 不能阻塞正在运行的事件循环；尽可能交给循环执行。
                    asyncio.create_task(result)