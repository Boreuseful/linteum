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
    from .session import Round, Session, empty_usage, utc_now
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
    from session import Round, Session, empty_usage, utc_now

ContextInjector = Callable[[str, list[Message]], Iterable[MessageInput]]


class ContextMiddleware:
    """在 LLM 请求前注入上下文，在响应后自动保存对话消息。

    注入顺序：静态注入内容 -> 动态 injector 内容 -> 持久化历史 -> 当前请求。
    注入内容（system prompt 等）放在历史**之前**，保证「assistant 的 tool_call」与
    「当前请求里的 tool 结果」始终相邻：Responses API 要求 ``function_call`` 紧跟
    ``function_call_output``，中间不能插入其它消息，否则会报
    ``No tool output found for tool call ...``。

    ``store`` 中保存的是 ``Session``（工作区 + 上下文表示 + 元数据），而不是
    provider 专用 JSON；每个会话的消息即 ``Session.context``。写入的消息与 usage 会
    并入当前进行中的轮（``begin_round()`` / ``end_round()``，**一轮 = 一次完整
    agent-loop**）；没有进行中的轮时，一次 ``complete()`` / ``stream()`` 就是一轮。
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
        # 注入内容放在历史之前：这样「assistant 的 tool_call（在历史末尾）」与
        # 「当前请求里的 tool 结果」不会被注入的 system 消息隔开——Responses API
        # 要求 function_call 紧跟 function_call_output。
        return injected + history + normalize_messages(messages)

    @staticmethod
    def _response_message(response: LLMResponse) -> AssistantMessage:
        return AssistantMessage(
            content=response.text or None,
            tool_calls=response.tool_calls,
            reasoning_content=response.reasoning_content or None,
        )

    def _save_turn(
        self,
        session_id: str,
        messages: MessageInputs,
        response: LLMResponse,
        *,
        created_at: str | None = None,
    ) -> None:
        """把「本次请求入参 + 本次回复」写入会话。

        这一组消息与 ``response.usage`` 会并入当前进行中的轮（``begin_round()`` 打开的
        完整 agent-loop）；没有进行中的轮时（单独使用 Middleware）会立即定稿成一轮。
        轮的 ``usage`` 汇总本轮的 token 消耗（同时累加到 ``session.usage``）、本轮占用
        的上下文空间（``context_tokens`` / ``context_chars``）以及起止时间。
        """
        session = self.ensure_session(session_id)
        items: list[Message] = []
        if self.persist_user_messages:
            items.extend(normalize_messages(messages))
        items.append(self._response_message(response))
        session.record_round(items, response.usage, created_at=created_at)

    def get_context(self, session_id: str = "default") -> list[Message]:
        return self.store.get(session_id)

    def clear_context(self, session_id: str = "default") -> None:
        self.store.clear(session_id)

    def append_tool_results(self, session_id: str, results: MessageInputs) -> None:
        self.store.append(session_id, results)

    def complete(self, messages: MessageInputs, *, session_id: str = "default", **kwargs: Any) -> LLMResponse:
        started_at = utc_now()
        response = self.client.complete(self._messages(session_id, messages), session_id=session_id, **kwargs)
        self._save_turn(session_id, messages, response, created_at=started_at)
        return response

    def stream(self, messages: MessageInputs, *, session_id: str = "default", **kwargs: Any) -> Iterator[LLMStreamChunk]:
        started_at = utc_now()
        final: LLMResponse | None = None
        for chunk in self.client.stream(self._messages(session_id, messages), session_id=session_id, **kwargs):
            if chunk.done and chunk.response:
                final = chunk.response
            yield chunk
        if final:
            self._save_turn(session_id, messages, final, created_at=started_at)

    async def acomplete(self, messages: MessageInputs, *, session_id: str = "default", **kwargs: Any) -> LLMResponse:
        started_at = utc_now()
        response = await self.client.acomplete(self._messages(session_id, messages), session_id=session_id, **kwargs)
        self._save_turn(session_id, messages, response, created_at=started_at)
        return response

    async def astream(self, messages: MessageInputs, *, session_id: str = "default", **kwargs: Any) -> AsyncIterator[LLMStreamChunk]:
        started_at = utc_now()
        final: LLMResponse | None = None
        async for chunk in self.client.astream(self._messages(session_id, messages), session_id=session_id, **kwargs):
            if chunk.done and chunk.response:
                final = chunk.response
            yield chunk
        if final:
            self._save_turn(session_id, messages, final, created_at=started_at)

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
        """返回会话累计的 token 用量（input/output/cache_hit/total）。

        会话存在时以独立字段 ``session.usage``（可持久化）为准，因此
        ``reset_session_usage()`` 之后读到的是归零结果；会话不存在时回退到
        ``LLMClient`` 的计数。
        """
        session = self.store.get_session(session_id)
        if session is not None:
            usage = empty_usage()
            usage.update(session.usage or {})
            return usage
        return dict(self.client.get_session_usage(session_id))

    def reset_session_usage(self, session_id: str = "default") -> Session | None:
        """把会话累计的 token 用量归零；会话不存在时返回 ``None``。"""
        session = self.store.get_session(session_id)
        if session is not None:
            session.reset_usage()
        return session

    def get_session_rounds(self, session_id: str = "default") -> list[Round]:
        """返回会话的分轮记录：每一轮 ``{context, usage}``；会话不存在时为空列表。

        一轮 = 一次完整 agent-loop：``context`` 是本轮全部消息（请求 + 每次 LLM 调用 +
        工具结果），``usage`` 是本轮输入/输出 token 汇总、本轮占用的上下文空间
        （``context_tokens`` / ``context_chars``）以及本轮起止时间
        （``created_at`` / ``updated_at``）。
        """
        session = self.store.get_session(session_id)
        return list(session.rounds) if session is not None else []

    # ------------------------------------------------------------------
    # 分轮（round）：一轮 = 一次完整 agent-loop
    # ------------------------------------------------------------------

    def begin_round(self, session_id: str = "default") -> Round:
        """开始一轮完整对话（一轮 = 一次 agent-loop）；已有进行中的轮时直接复用。

        ``AgentEngine`` 在工具循环开始前调用本方法、循环结束后调用 ``end_round()``，
        因此这一轮覆盖「一次请求 -> 多次 LLM 调用 + 工具执行 -> 最终回答」；期间
        ``complete()`` / ``stream()`` 写入的消息与 usage 都会并入这一轮。
        """
        return self.ensure_session(session_id).begin_round()

    def end_round(self, session_id: str = "default") -> Round | None:
        """结束进行中的轮（把本轮消息 + usage 定稿到会话）；没有则返回 ``None``。"""
        session = self.store.get_session(session_id)
        return session.end_round() if session is not None else None

    def open_round(self, session_id: str = "default") -> Round | None:
        """返回进行中的轮（尚未 ``end_round()``）；没有时返回 ``None``。"""
        session = self.store.get_session(session_id)
        return session.open_round if session is not None else None
