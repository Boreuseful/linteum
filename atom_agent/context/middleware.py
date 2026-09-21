from __future__ import annotations

from collections.abc import Callable, Iterable
from pathlib import Path
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
    from .store import ContextStore
    from .session import Session
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
    from store import ContextStore
    from session import Session

ContextInjector = Callable[[str, list[Message]], Iterable[MessageInput]]


class ContextMiddleware:
    """在 LLM 请求前注入上下文，在响应后自动保存对话消息。

    注入顺序：持久化历史 -> 静态注入内容 -> 动态 injector 内容 -> 当前请求。
    ``store`` 中保存的是 ``Session``（工作区 + 上下文表示 + 元数据），而不是
    provider 专用 JSON；每个会话的消息即 ``Session.context``。
    """

    def __init__(
        self,
        client: LLMClient,
        store: Any | None = None,
        *,
        context: MessageInputs | None = None,
        injector: ContextInjector | None = None,
        persist_user_messages: bool = True,
        workspace: str | Path | None = None,
    ):
        self.client = client
        self.store = store or ContextStore(workspace=workspace)
        self.workspace = (
            Path(workspace).expanduser().resolve() if workspace is not None else self.store.default_workspace
        )
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
        return AssistantMessage(
            content=response.text or None,
            tool_calls=response.tool_calls,
            reasoning_content=response.reasoning_content or None,
        )

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

    # ------------------------------------------------------------------
    # Session（工作区 + 上下文表示 + 元数据）
    # ------------------------------------------------------------------

    def ensure_session(
        self,
        session_id: str = "default",
        *,
        workspace: str | Path | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Session:
        """获取会话；不存在时按 middleware 默认工作区创建。"""
        return self.store.ensure_session(
            session_id,
            workspace=self.workspace if workspace is None else workspace,
            metadata=metadata,
        )

    def get_session(self, session_id: str = "default") -> Session | None:
        """返回会话对象；不存在时为 ``None``（不会创建会话）。"""
        return self.store.get_session(session_id)

    def session_workspace(self, session_id: str = "default") -> Path:
        """返回会话生效的工作区。"""
        return self.store.workspace(session_id)

    def set_workspace(self, session_id: str, workspace: str | Path | None) -> Session:
        """设置会话工作区；``None`` 表示恢复为“运行时继承”。"""
        return self.store.set_workspace(session_id, workspace)

    def get_session_usage(self, session_id: str = "default") -> dict[str, int]:
        return self.client.get_session_usage(session_id)
