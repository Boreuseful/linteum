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
except ImportError:  # supports adding the linteum_agent directory to sys.path directly
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
    # Layer 1: Agent lifecycle
    AgentStartEvent,
    AgentEndEvent,

    # Layer 2: Turn lifecycle
    TurnStartEvent,
    TurnEndEvent,

    # Layer 3: Message lifecycle
    MessageStartEvent,
    MessageUpdateEvent,
    MessageEndEvent,

    # Layer 4: Tool Execution lifecycle
    ToolExecutionStartEvent,
    ToolExecutionUpdateEvent,
    ToolExecutionEndEvent,
]

class AgentEventListener:
    """
    The full event listener interface.
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
        Listen by event type.

        For example:
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
        Receive all AgentEvents.
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
        """Synchronous version, used by ``AgentEngine.stream`` and ``run``."""
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
                    # A synchronous API must not block a running event loop; hand it to the loop whenever possible.
                    asyncio.create_task(result)