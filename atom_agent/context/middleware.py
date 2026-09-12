from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any, AsyncIterator, Iterator

try:
    from ..model.client import LLMClient
    from ..model.llm import LLMResponse, LLMStreamChunk
    from ..messages.agent_message import (
        AssistantMessage,
        Message,
        MessageInput,
        MessageInputs,
        ToolCall,
        UserPrompt,
        normalize_messages,
        to_dict,
    )
except ImportError:  # 支持直接将 /workspace/atom_agent 加入 sys.path
    from model.client import LLMClient
    from model.llm import LLMResponse, LLMStreamChunk
    from messages.agent_message import (
        AssistantMessage,
        Message,
        MessageInput,
        MessageInputs,
        ToolCall,
        UserPrompt,
        normalize_messages,
        to_dict,
    )

ContextInjector = Callable[[str, list[Message]], Iterable[MessageInput]]


class ContextMiddleware:
    """在 LLM 请求前注入上下文，在响应后自动保存对话消息。

    注入顺序：持久化历史 -> 静态注入内容 -> 动态 injector 内容 -> 当前请求。
    ``store`` 中保存的是消息类对象，而不是 provider 专用 JSON。
    """

    def __init__(
        self,
        client: LLMClient,
        store: Any | None = None,
        *,
        context: MessageInputs | None = None,
        injector: ContextInjector | None = None,
        persist_user_messages: bool = True,
    ):
        from .store import ContextStore

        self.client = client
        self.store = store or ContextStore()
        self.context = normalize_messages(context or [])
        self.injector = injector
        self.persist_user_messages = persist_user_messages

    def _messages(self, session_id: str, messages: MessageInputs) -> list[Message]:
        history = self.store.get(session_id)
        injected = list(self.context)
        if self.injector:
            injected.extend(normalize_messages(self.injector(session_id, history)))
        return history + injected + normalize_messages(messages)

    @staticmethod
    def _response_message(response: LLMResponse) -> AssistantMessage:
        return AssistantMessage(content=response.text or None, tool_calls=response.tool_calls)

    def _save_turn(self, session_id: str, messages: MessageInputs, response: LLMResponse) -> None:
        if self.persist_user_messages:
            self.store.append(session_id, messages)
        self.store.append(session_id, self._response_message(response))

    def get_context(self, session_id: str = "default") -> list[Message]:
        return self.store.get(session_id)

    def clear_context(self, session_id: str = "default") -> None:
        self.store.clear(session_id)

    def complete(self, messages: MessageInputs, *, session_id: str = "default", **kwargs: Any) -> LLMResponse:
        response = self.client.complete(self._messages(session_id, messages), session_id=session_id, **kwargs)
        self._save_turn(session_id, messages, response)
        return response

    def stream(self, messages: MessageInputs, *, session_id: str = "default", **kwargs: Any) -> Iterator[LLMStreamChunk]:
        final: LLMResponse | None = None
        for chunk in self.client.stream(self._messages(session_id, messages), session_id=session_id, **kwargs):
            if chunk.done and chunk.response:
                final = chunk.response
            yield chunk
        if final:
            self._save_turn(session_id, messages, final)

    async def acomplete(self, messages: MessageInputs, *, session_id: str = "default", **kwargs: Any) -> LLMResponse:
        response = await self.client.acomplete(self._messages(session_id, messages), session_id=session_id, **kwargs)
        self._save_turn(session_id, messages, response)
        return response

    async def astream(self, messages: MessageInputs, *, session_id: str = "default", **kwargs: Any) -> AsyncIterator[LLMStreamChunk]:
        final: LLMResponse | None = None
        async for chunk in self.client.astream(self._messages(session_id, messages), session_id=session_id, **kwargs):
            if chunk.done and chunk.response:
                final = chunk.response
            yield chunk
        if final:
            self._save_turn(session_id, messages, final)

    def get_session_usage(self, session_id: str = "default") -> dict[str, int]:
        return self.client.get_session_usage(session_id)