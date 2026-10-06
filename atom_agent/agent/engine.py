from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Any, AsyncIterator, Iterator

try:
    from ..context.middleware import ContextMiddleware
    from ..context.session import Round, Session
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
    from context.session import Round, Session
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

try:
    from evnts import (AgentStartEvent,
                                   AgentEndEvent,
                                   TurnStartEvent,
                                   TurnEndEvent,
                                   MessageStartEvent,
                                   MessageUpdateEvent,
                                   MessageEndEvent,
                                   ToolExecutionStartEvent,
                                   ToolExecutionUpdateEvent,
                                   ToolExecutionEndEvent)
    from ..evnts import EventBus

except ImportError:
    from ..evnts import (AgentStartEvent,
                                       AgentEndEvent,
                                       TurnStartEvent,
                                       TurnEndEvent,
                                       MessageStartEvent,
                                       MessageUpdateEvent,
                                       MessageEndEvent,
                                       ToolExecutionStartEvent,
                                       ToolExecutionUpdateEvent,
                                       ToolExecutionEndEvent)
    from ..evnts import EventBus

class MaxToolRoundsError(RuntimeError):
    """模型在 ``max_tool_rounds`` 轮内仍未结束工具调用时抛出。

    ``RuntimeError`` 的子类：既保留引擎「超轮次即中断」的契约，也方便宿主（CLI / TUI /
    Web）按类型识别并做优雅处理，而无需匹配错误文本。
    """

    DEFAULT_MESSAGE = "工具调用超过最大轮次"

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message or self.DEFAULT_MESSAGE)


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
    # 本次执行使用的会话工作区（来自 Session；没有工具注册表时为 None）。
    workspace: Path | None = None

    @property
    def output(self) -> Message:
        return self.context[-1]

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "text": self.text,
            "context": [to_dict(message) for message in self.context],
            "usage": self.usage,
            "workspace": str(self.workspace) if self.workspace is not None else None,
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
    # 本次增量中的思维链文本。
    reasoning_delta: str = ""


class AgentEngine:
    """将输入 context -> LLM -> 输出 context 串联起来的 Agent 引擎。"""

    def __init__(
        self,
        client: LLMClient,
        middleware: ContextMiddleware | None = None,
        *,
        default_context: MessageInputs | None = None,
        tool_registry: ToolRegistry | None = None,
        events_bus: EventBus | None = None,
        max_tool_rounds: int = 8,
    ):
        self.client = client
        self.middleware = middleware or ContextMiddleware(client)
        self.default_context = normalize_messages(default_context or [])
        self.tool_registry = tool_registry
        self.events_bus = events_bus or EventBus()
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

    def _bind_tool_context(
        self,
        registry: ToolRegistry | None,
        session_id: str,
        metadata: dict[str, Any] | None = None,
    ) -> Session | None:
        """把会话绑定到工具注册表，让工具使用会话的工作区与工具配置。

        - 会话工作区未显式指定时，继承注册表当前会话的工作区（保持旧行为）；
        - 把注册表中的工具记录进会话（``session.registered_tools()``）；
        - 本次调用 ``metadata`` 与会话元数据一起交给工具（会话元数据优先）；
        - 保留注册表原有的 ``approve``/``cancelled`` 回调。
        """
        if registry is None:
            return None
        session = self.middleware.ensure_session(session_id)
        previous = getattr(registry, "session", None) or getattr(registry, "context", None)
        if session.workspace is None and previous is not None:
            session.set_workspace(previous.effective_workspace)
        # 记录本会话注册的工具，再生成会话视图，让视图也能读到 registered 列表。
        session.register_tools(registry.all())
        context_metadata = dict(getattr(previous, "metadata", {}) or {})
        context_metadata.update(metadata or {})
        registry.session = session.tool_context(
            metadata=context_metadata,
            approve=getattr(previous, "approve", None),
            cancelled=getattr(previous, "cancelled", None),
        )
        return session

    def _wrap_response(
        self,
        request: AgentContext,
        response: LLMResponse,
        context: list[Message] | None = None,
        session: Session | None = None,
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
                    reasoning_content=response.reasoning_content or None,
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
            workspace=session.workspace if session is not None else None,
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
            arguments = self._arguments(call)
            self.events_bus.emit_sync({"type": "tool_execution_start", "tool_call_id": call.id,
                                       "tool_name": call.name, "args": arguments})
            self.events_bus.emit_sync({"type": "tool_execution_update", "tool_call_id": call.id,
                                       "tool_name": call.name, "args": arguments,
                                       "partial_result": "正在执行工具..."})
            try:
                arguments = self._arguments(call)
                value = registry.execute_sync(call.name, arguments)
                if isinstance(value, ExecutionToolResult):
                    value = {"tool_name": call.name, "content": value.content,
                             "is_error": value.is_error, "details": value.details}
            except Exception as exc:
                value = {"tool_name": call.name, "is_error": True, "content": str(exc), "details": None}
            self.events_bus.emit_sync({"type": "tool_execution_end", "tool_call_id": call.id,
                                       "tool_name": call.name, "result": value["content"],
                                       "is_error": value["is_error"]})
            results.append(MessageToolResult(call.id,**value))
        return results

    async def _tool_results_async(
        self,
        response: LLMResponse,
        registry: ToolRegistry,
    ) -> list[MessageToolResult]:
        results: list[MessageToolResult] = []
        for call in response.tool_calls:
            arguments = self._arguments(call)
            await self.events_bus.emit({"type": "tool_execution_start", "tool_call_id": call.id,
                                        "tool_name": call.name, "args": arguments})
            await self.events_bus.emit({"type": "tool_execution_update", "tool_call_id": call.id,
                                        "tool_name": call.name, "args": arguments,
                                        "partial_result": "正在执行工具..."})
            try:
                value = await registry.execute(call.name, arguments)
                if isinstance(value, ExecutionToolResult):
                    value = {"tool_name": call.name, "content": value.content,
                             "is_error": value.is_error, "details": value.details}
            except Exception as exc:
                value = {"tool_name": call.name, "is_error": True, "content": str(exc), "details": None}
            await self.events_bus.emit({"type": "tool_execution_end", "tool_call_id": call.id,
                                        "tool_name": call.name, "result": value["content"],
                                        "is_error": value["is_error"]})
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
                reasoning_content=response.reasoning_content or None,
            )
        )
        context.extend(results)

    def _record_tool_results(self, session_id: str, results: list[MessageToolResult]) -> None:
        if results:
            self.middleware.append_tool_results(session_id, results)

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
        self.events_bus.emit_sync({"type": "agent_start"})
        self.events_bus.emit_sync({"type": "message_start", "message": request.messages()[-1]})
        registry = tool_registry or self.tool_registry
        kwargs.pop("tools", None)
        # 先绑定会话（工作区 + 工具配置），再生成工具定义，
        # 这样会话中禁用的工具不会被提供给 LLM。
        session = self._bind_tool_context(registry, session_id, metadata)
        self._tool_definitions(registry, kwargs)
        # 一轮 = 一次完整 agent-loop：循环开始前打开轮，循环结束（含异常）后定稿。
        self.middleware.begin_round(session_id)
        try:
            response = self.middleware.complete(request.messages(), session_id=session_id, **kwargs)
            execution_context = request.messages()
            rounds = max_tool_rounds if max_tool_rounds is not None else self.max_tool_rounds
            for round_index in range(rounds + 1):
                self.events_bus.emit_sync({"type": "turn_start"})
                if not response.tool_calls or registry is None:
                    result = self._wrap_response(request, response, execution_context, session)
                    self.events_bus.emit_sync({"type": "message_start", "message": result.output})
                    self.events_bus.emit_sync({"type": "message_end", "message": result.output})
                    self.events_bus.emit_sync({"type": "turn_end", "message": result.output, "tool_results": []})
                    return result
                results = self._tool_results(response, registry)
                self._append_tool_round(execution_context, response, results)
                self.events_bus.emit_sync({"type": "turn_end", "message": execution_context[-len(results)-1],
                                           "tool_results": results})
                if round_index >= rounds:
                    self._record_tool_results(session_id, results)
                    raise MaxToolRoundsError()
                response = self.middleware.complete(results, session_id=session_id, **kwargs)
            raise MaxToolRoundsError()
        finally:
            self.middleware.end_round(session_id)
            self.events_bus.emit_sync({"type": "agent_end", "messages": execution_context if 'execution_context' in locals() else []})

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
        await self.events_bus.emit({"type": "agent_start"})
        await self.events_bus.emit({"type": "message_start", "message": request.messages()[-1]})
        registry = tool_registry or self.tool_registry
        kwargs.pop("tools", None)
        # 先绑定会话（工作区 + 工具配置），再生成工具定义，
        # 这样会话中禁用的工具不会被提供给 LLM。
        session = self._bind_tool_context(registry, session_id, metadata)
        self._tool_definitions(registry, kwargs)
        # 一轮 = 一次完整 agent-loop：循环开始前打开轮，循环结束（含异常）后定稿。
        self.middleware.begin_round(session_id)
        try:
            response = await self.middleware.acomplete(request.messages(), session_id=session_id, **kwargs)
            execution_context = request.messages()
            rounds = max_tool_rounds if max_tool_rounds is not None else self.max_tool_rounds
            for round_index in range(rounds + 1):
                await self.events_bus.emit({"type": "turn_start"})
                if not response.tool_calls or registry is None:
                    result = self._wrap_response(request, response, execution_context, session)
                    await self.events_bus.emit({"type": "message_start", "message": result.output})
                    await self.events_bus.emit({"type": "message_end", "message": result.output})
                    await self.events_bus.emit({"type": "turn_end", "message": result.output, "tool_results": []})
                    return result
                results = await self._tool_results_async(response, registry)
                self._append_tool_round(execution_context, response, results)
                await self.events_bus.emit({"type": "turn_end", "message": execution_context[-len(results)-1],
                                            "tool_results": results})
                if round_index >= rounds:
                    self._record_tool_results(session_id, results)
                    raise MaxToolRoundsError()
                response = await self.middleware.acomplete(results, session_id=session_id, **kwargs)
            raise MaxToolRoundsError()
        finally:
            self.middleware.end_round(session_id)
            await self.events_bus.emit({"type": "agent_end", "messages": execution_context if 'execution_context' in locals() else []})

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
        self.events_bus.emit_sync({"type": "agent_start"})
        self.events_bus.emit_sync({"type": "message_start", "message": request.messages()[-1]})
        registry = tool_registry or self.tool_registry
        kwargs.pop("tools", None)
        # 先绑定会话（工作区 + 工具配置），再生成工具定义，
        # 这样会话中禁用的工具不会被提供给 LLM。
        session = self._bind_tool_context(registry, session_id, metadata)
        self._tool_definitions(registry, kwargs)
        # 一轮 = 一次完整 agent-loop：循环开始前打开轮，循环结束（含异常 / 提前关闭）
        # 后定稿，因此这一轮覆盖「请求 -> 多次 LLM 调用 + 工具执行 -> 最终回答」。
        self.middleware.begin_round(session_id)
        messages: MessageInputs = request.messages()
        execution_context = request.messages()
        rounds = max_tool_rounds if max_tool_rounds is not None else self.max_tool_rounds
        try:
            for round_index in range(rounds + 1):
                self.events_bus.emit_sync({"type": "turn_start"})
                final_chunk: LLMStreamChunk | None = None
                for chunk in self.middleware.stream(messages, session_id=session_id, **kwargs):
                    if chunk.done:
                        final_chunk = chunk
                        continue
                    if chunk.reasoning_content:
                        self.events_bus.emit_sync({
                            "type": "message_update",
                            "message": AssistantMessage(reasoning_content=chunk.reasoning_content),
                            "assistant_message_event": {"type": "reasoning_delta", "text": chunk.reasoning_content},
                        })
                    if chunk.delta:
                        self.events_bus.emit_sync({
                            "type": "message_update",
                            "message": AssistantMessage(content=chunk.delta),
                            "assistant_message_event": {"type": "text_delta", "text": chunk.delta},
                        })
                    if chunk.reasoning_content or chunk.delta:
                        yield AgentStreamChunk(
                            delta=chunk.delta,
                            reasoning_delta=chunk.reasoning_content,
                            raw_chunk=chunk,
                        )

                if final_chunk is None or final_chunk.response is None:
                    self.events_bus.emit_sync({"type": "agent_end", "messages": execution_context})
                    return
                response = final_chunk.response
                if not response.tool_calls or registry is None:
                    result = self._wrap_response(request, response, execution_context, session)
                    self.events_bus.emit_sync({"type": "message_start", "message": result.output})
                    self.events_bus.emit_sync({"type": "message_end", "message": result.output})
                    self.events_bus.emit_sync({"type": "turn_end", "message": result.output, "tool_results": []})
                    yield AgentStreamChunk(
                        done=True,
                        usage=final_chunk.usage,
                        result=result,
                        raw_chunk=final_chunk,
                    )
                    return

                results = self._tool_results(response, registry)
                self._append_tool_round(execution_context, response, results)
                self.events_bus.emit_sync({"type": "turn_end", "message": execution_context[-len(results)-1],
                                           "tool_results": results})
                if round_index >= rounds:
                    self._record_tool_results(session_id, results)
                    raise MaxToolRoundsError()
                messages = results

            raise MaxToolRoundsError()
        finally:
            self.middleware.end_round(session_id)

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
        await self.events_bus.emit({"type": "agent_start"})
        await self.events_bus.emit({"type": "message_start", "message": request.messages()[-1]})
        registry = tool_registry or self.tool_registry
        kwargs.pop("tools", None)
        # 先绑定会话（工作区 + 工具配置），再生成工具定义，
        # 这样会话中禁用的工具不会被提供给 LLM。
        session = self._bind_tool_context(registry, session_id, metadata)
        self._tool_definitions(registry, kwargs)

        # 一轮 = 一次完整 agent-loop：循环开始前打开轮，循环结束（含异常 / 提前关闭）
        # 后定稿，因此这一轮覆盖「请求 -> 多次 LLM 调用 + 工具执行 -> 最终回答」。
        self.middleware.begin_round(session_id)
        messages: MessageInputs = request.messages()
        execution_context = request.messages()
        rounds = max_tool_rounds if max_tool_rounds is not None else self.max_tool_rounds
        try:
            for round_index in range(rounds + 1):
                await self.events_bus.emit({"type": "turn_start"})
                final_chunk: LLMStreamChunk | None = None
                async for chunk in self.middleware.astream(messages, session_id=session_id, **kwargs):
                    if chunk.done:
                        final_chunk = chunk
                        continue
                    if chunk.reasoning_content:
                        await self.events_bus.emit({
                            "type": "message_update",
                            "message": AssistantMessage(reasoning_content=chunk.reasoning_content),
                            "assistant_message_event": {"type": "reasoning_delta", "text": chunk.reasoning_content},
                        })
                    if chunk.delta:
                        await self.events_bus.emit({
                            "type": "message_update",
                            "message": AssistantMessage(content=chunk.delta),
                            "assistant_message_event": {"type": "text_delta", "text": chunk.delta},
                        })
                    if chunk.reasoning_content or chunk.delta:
                        yield AgentStreamChunk(
                            delta=chunk.delta,
                            reasoning_delta=chunk.reasoning_content,
                            raw_chunk=chunk,
                        )

                if final_chunk is None or final_chunk.response is None:
                    await self.events_bus.emit({"type": "agent_end", "messages": execution_context})
                    return
                response = final_chunk.response
                if not response.tool_calls or registry is None:
                    result = self._wrap_response(request, response, execution_context, session)
                    await self.events_bus.emit({"type": "message_start", "message": result.output})
                    await self.events_bus.emit({"type": "message_end", "message": result.output})
                    await self.events_bus.emit({"type": "turn_end", "message": result.output, "tool_results": []})
                    yield AgentStreamChunk(
                        done=True,
                        usage=final_chunk.usage,
                        result=result,
                        raw_chunk=final_chunk,
                    )
                    return

                results = await self._tool_results_async(response, registry)
                self._append_tool_round(execution_context, response, results)
                await self.events_bus.emit({"type": "turn_end", "message": execution_context[-len(results)-1],
                                            "tool_results": results})
                if round_index >= rounds:
                    self._record_tool_results(session_id, results)
                    raise MaxToolRoundsError()
                messages = results

            raise MaxToolRoundsError()
        finally:
            self.middleware.end_round(session_id)

    def get_context(self, session_id: str = "default") -> list[Message]:
        return self.middleware.get_context(session_id)

    def get_session(self, session_id: str = "default") -> Session | None:
        """返回会话（工作区 + 上下文表示 + 元数据）；不存在时为 ``None``。"""
        return self.middleware.get_session(session_id)

    def ensure_session(self, session_id: str = "default", *, workspace: str | Path | None = None) -> Session:
        """获取会话；不存在时创建。"""
        return self.middleware.ensure_session(session_id, workspace=workspace)

    def get_workspace(self, session_id: str = "default") -> Path:
        """返回会话生效的工作区。"""
        return self.middleware.session_workspace(session_id)

    def set_workspace(self, session_id: str, workspace: str | Path | None) -> Session:
        """设置会话工作区；``None`` 表示恢复为“运行时继承”。"""
        return self.middleware.set_workspace(session_id, workspace)

    def get_session_usage(self, session_id: str = "default") -> dict[str, int]:
        """返回会话累计 token usage（input/output/cache_hit/total）。"""
        return self.middleware.get_session_usage(session_id)

    def reset_session_usage(self, session_id: str = "default") -> Session | None:
        """把会话累计 token usage 归零；会话不存在时返回 ``None``。"""
        return self.middleware.reset_session_usage(session_id)

    def get_session_rounds(self, session_id: str = "default") -> list[Round]:
        """返回会话的分轮记录：每一轮 ``{context, usage}``（本轮 token 消耗 + 占用空间 + 起止时间）。"""
        return self.middleware.get_session_rounds(session_id)
