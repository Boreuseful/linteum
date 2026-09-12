from __future__ import annotations

from dataclasses import dataclass, field
import json
from typing import Any, AsyncIterator, Iterator

try:
    from ..context.middleware import ContextMiddleware
    from ..model.client import LLMClient
    from ..model.llm import LLMResponse, LLMStreamChunk
    from ..messages.agent_message import (
        Message,
        MessageInputs,
        AssistantMessage,
        ToolCall,
        normalize_messages,
        to_dict,
    )
except ImportError:  # 支持直接将 /workspace/atom_agent 加入 sys.path
    from context.middleware import ContextMiddleware
    from model.client import LLMClient
    from model.llm import LLMResponse, LLMStreamChunk
    from messages.agent_message import AssistantMessage, Message, MessageInputs, ToolCall, normalize_messages, to_dict

try:
    from ..messages.agent_message import ToolResult as MessageToolResult
    from ..tools.core import ToolResult as ExecutionToolResult
    from ..tools.registry import ToolRegistry
    from ..tools.builtin import tool_result_content
except ImportError:
    from messages.agent_message import ToolResult as MessageToolResult
    from tools.core import ToolResult as ExecutionToolResult
    from tools.registry import ToolRegistry
    from tools.builtin import tool_result_content

@dataclass
class AgentContext:
    """一次 Agent 执行的输入上下文。"""

    prompt: MessageInputs
    context: MessageInputs | None = None
    session_id: str = "default"
    metadata: dict[str, Any] = field(default_factory=dict)

    def messages(self) -> list[Message]:
        """将 context 和 prompt 统一为 provider-neutral 的消息对象。"""
        return normalize_messages(self.context or []) + normalize_messages(self.prompt)

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "context": [to_dict(message) for message in normalize_messages(self.context or [])],
            "prompt": [to_dict(message) for message in normalize_messages(self.prompt)],
            "metadata": self.metadata,
        }


@dataclass
class AgentResult:
    """LLM 输出重新包装后的 Agent 结果。"""

    text: str
    context: list[Message]
    session_id: str
    usage: dict[str, int] = field(default_factory=dict)
    tool_calls: list[ToolCall] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    response: LLMResponse | None = None

    @property
    def output(self) -> Message:
        return self.context[-1]

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "text": self.text,
            "context": [to_dict(message) for message in self.context],
            "usage": self.usage,
            "tool_calls": [
                {"id": call.id, "name": call.name, "arguments": call.arguments}
                for call in self.tool_calls
            ],
            "metadata": self.metadata,
        }


@dataclass
class AgentStreamChunk:
    """流式 Agent 输出；结束时 ``result`` 包含完整的 AgentResult。"""

    delta: str = ""
    done: bool = False
    usage: dict[str, int] = field(default_factory=dict)
    result: AgentResult | None = None
    raw_chunk: LLMStreamChunk | None = None


class AgentEngine:
    """将输入 context -> LLM -> 输出 context 串联起来的 Agent 引擎。"""

    def __init__(
        self,
        client: LLMClient,
        middleware: ContextMiddleware | None = None,
        *,
        default_context: MessageInputs | None = None,
        tool_registry: ToolRegistry | None = None,
        max_tool_rounds: int = 8,
    ):
        self.client = client
        self.middleware = middleware or ContextMiddleware(client)
        self.default_context = normalize_messages(default_context or [])
        self.tool_registry = tool_registry
        self.max_tool_rounds = max_tool_rounds

    def _request_context(
        self,
        prompt: MessageInputs,
        context: MessageInputs | None,
        session_id: str,
        metadata: dict[str, Any] | None,
    ) -> AgentContext:
        request_context = list(self.default_context)
        request_context.extend(normalize_messages(context or []))
        return AgentContext(
            prompt=prompt,
            context=request_context,
            session_id=session_id,
            metadata=dict(metadata or {}),
        )

    def _wrap_response(
        self,
        request: AgentContext,
        response: LLMResponse,
        context: list[Message] | None = None,
    ) -> AgentResult:
        # middleware 已经把 response 包装成 AssistantMessage 并保存到 session；
        # 这里返回当前请求输入和本次 assistant 输出组成的结构化 context。
        output_context = list(context or request.messages())
        if not response.tool_calls or not output_context or not (
            isinstance(output_context[-1], AssistantMessage)
            and output_context[-1].tool_calls == response.tool_calls
        ):
            output_context.append(
                AssistantMessage(
                    content=response.text or None,
                    tool_calls=response.tool_calls,
                )
            )
        return AgentResult(
            text=response.text,
            context=output_context,
            session_id=request.session_id,
            usage=dict(response.usage),
            tool_calls=list(response.tool_calls),
            metadata=dict(request.metadata),
            response=response,
        )

    @staticmethod
    def _tool_definitions(
        registry: ToolRegistry | None,
        kwargs: dict[str, Any],
    ) -> None:
        """将注册表转换成 LLM tools；显式 registry 优先于 tools 参数。"""
        if registry is not None:
            kwargs["tools"] = registry.definitions()

    def _tool_results(
        self,
        response: LLMResponse,
        registry: ToolRegistry,
    ) -> list[MessageToolResult]:
        results: list[MessageToolResult] = []
        for call in response.tool_calls:
            try:
                value = registry.execute_sync(call.name, self._arguments(call))
                if isinstance(value, ExecutionToolResult):
                    value = {"tool_name":call.name,"content": value.content, "is_error": value.is_error, "details": value.details}
            except Exception as exc:
                value = {"tool_name":call.name,"is_error": value.is_error, "content": str(exc),"details": value.details}
            results.append(MessageToolResult(call.id,**value))
        return results

    async def _tool_results_async(
        self,
        response: LLMResponse,
        registry: ToolRegistry,
    ) -> list[MessageToolResult]:
        results: list[MessageToolResult] = []
        for call in response.tool_calls:
            try:
                value = await registry.execute(call.name, self._arguments(call))
                if isinstance(value, ExecutionToolResult):
                    value = {"tool_name":call.name,"content": value.content, "is_error": value.is_error, "details": value.details}
            except Exception as exc:
                value = {"tool_name":call.name,"is_error": value.is_error, "content": str(exc),"details": value.details}
            results.append(MessageToolResult(call.id,**value))
        return results

    @staticmethod
    def _append_tool_round(
        context: list[Message],
        response: LLMResponse,
        results: list[MessageToolResult],
    ) -> None:
        context.append(
            AssistantMessage(
                content=response.text or None,
                tool_calls=list(response.tool_calls),
            )
        )
        context.extend(results)

    def run(
        self,
        prompt: MessageInputs,
        *,
        context: MessageInputs | None = None,
        session_id: str = "default",
        metadata: dict[str, Any] | None = None,
        tool_registry: ToolRegistry | None = None,
        max_tool_rounds: int | None = None,
        **kwargs: Any,
    ) -> AgentResult:
        request = self._request_context(prompt, context, session_id, metadata)
        registry = tool_registry or self.tool_registry
        kwargs.pop("tools", None)
        self._tool_definitions(registry, kwargs)
        response = self.middleware.complete(request.messages(), session_id=session_id, **kwargs)
        execution_context = request.messages()
        rounds = max_tool_rounds if max_tool_rounds is not None else self.max_tool_rounds
        for round_index in range(rounds + 1):
            if not response.tool_calls:
                break
            if registry is None:
                break
            if round_index >= rounds:
                raise RuntimeError("工具调用超过最大轮次")
            results = self._tool_results(response, registry)
            self._append_tool_round(execution_context, response, results)
            response = self.middleware.complete(results, session_id=session_id, **kwargs)
        return self._wrap_response(request, response, execution_context)

    async def arun(
        self,
        prompt: MessageInputs,
        *,
        context: MessageInputs | None = None,
        session_id: str = "default",
        metadata: dict[str, Any] | None = None,
        tool_registry: ToolRegistry | None = None,
        max_tool_rounds: int | None = None,
        **kwargs: Any,
    ) -> AgentResult:
        request = self._request_context(prompt, context, session_id, metadata)
        registry = tool_registry or self.tool_registry
        kwargs.pop("tools", None)
        self._tool_definitions(registry, kwargs)
        response = await self.middleware.acomplete(request.messages(), session_id=session_id, **kwargs)
        execution_context = request.messages()
        rounds = max_tool_rounds if max_tool_rounds is not None else self.max_tool_rounds
        for round_index in range(rounds + 1):
            if not response.tool_calls:
                break
            if registry is None:
                break
            if round_index >= rounds:
                raise RuntimeError("工具调用超过最大轮次")
            results = await self._tool_results_async(response, registry)
            self._append_tool_round(execution_context, response, results)
            response = await self.middleware.acomplete(results, session_id=session_id, **kwargs)
        return self._wrap_response(request, response, execution_context)

    @staticmethod
    def _arguments(call: ToolCall) -> dict[str, Any]:
        if isinstance(call.arguments, dict):
            return call.arguments
        try:
            value = json.loads(call.arguments)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError(f"工具参数不是有效 JSON: {call.name}") from exc
        if not isinstance(value, dict):
            raise ValueError(f"工具参数必须是对象: {call.name}")
        return value

    def stream(
        self,
        prompt: MessageInputs,
        *,
        context: MessageInputs | None = None,
        session_id: str = "default",
        metadata: dict[str, Any] | None = None,
        tool_registry: ToolRegistry | None = None,
        max_tool_rounds: int | None = None,
        **kwargs: Any,
    ) -> Iterator[AgentStreamChunk]:
        request = self._request_context(prompt, context, session_id, metadata)
        registry = tool_registry or self.tool_registry
        kwargs.pop("tools", None)
        self._tool_definitions(registry, kwargs)

        messages: MessageInputs = request.messages()
        execution_context = request.messages()
        rounds = max_tool_rounds if max_tool_rounds is not None else self.max_tool_rounds
        for round_index in range(rounds + 1):
            final_chunk: LLMStreamChunk | None = None
            for chunk in self.middleware.stream(messages, session_id=session_id, **kwargs):
                if chunk.done:
                    final_chunk = chunk
                else:
                    yield AgentStreamChunk(delta=chunk.delta, raw_chunk=chunk)

            if final_chunk is None or final_chunk.response is None:
                return
            response = final_chunk.response
            if not response.tool_calls or registry is None:
                yield AgentStreamChunk(
                    done=True,
                    usage=final_chunk.usage,
                    result=self._wrap_response(request, response, execution_context),
                    raw_chunk=final_chunk,
                )
                return

            if round_index >= rounds:
                raise RuntimeError("工具调用超过最大轮次")
            results = self._tool_results(response, registry)
            self._append_tool_round(execution_context, response, results)
            messages = results

        raise RuntimeError("工具调用超过最大轮次")

    async def astream(
        self,
        prompt: MessageInputs,
        *,
        context: MessageInputs | None = None,
        session_id: str = "default",
        metadata: dict[str, Any] | None = None,
        tool_registry: ToolRegistry | None = None,
        max_tool_rounds: int | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[AgentStreamChunk]:
        request = self._request_context(prompt, context, session_id, metadata)
        registry = tool_registry or self.tool_registry
        kwargs.pop("tools", None)
        self._tool_definitions(registry, kwargs)

        messages: MessageInputs = request.messages()
        execution_context = request.messages()
        rounds = max_tool_rounds if max_tool_rounds is not None else self.max_tool_rounds
        for round_index in range(rounds + 1):
            final_chunk: LLMStreamChunk | None = None
            async for chunk in self.middleware.astream(messages, session_id=session_id, **kwargs):
                if chunk.done:
                    final_chunk = chunk
                else:
                    yield AgentStreamChunk(delta=chunk.delta, raw_chunk=chunk)

            if final_chunk is None or final_chunk.response is None:
                return
            response = final_chunk.response
            if not response.tool_calls or registry is None:
                yield AgentStreamChunk(
                    done=True,
                    usage=final_chunk.usage,
                    result=self._wrap_response(request, response, execution_context),
                    raw_chunk=final_chunk,
                )
                return

            if round_index >= rounds:
                raise RuntimeError("工具调用超过最大轮次")
            results = await self._tool_results_async(response, registry)
            self._append_tool_round(execution_context, response, results)
            messages = results

        raise RuntimeError("工具调用超过最大轮次")

    def get_context(self, session_id: str = "default") -> list[Message]:
        return self.middleware.get_context(session_id)

    def get_session_usage(self, session_id: str = "default") -> dict[str, int]:
        return self.middleware.get_session_usage(session_id)