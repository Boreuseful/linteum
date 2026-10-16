"""OpenAI adapter: Responses API **and** Chat Completions via the ``openai`` SDK.

One class covers the whole OpenAI family.  ``config.openai_mode`` chooses the
dialect; ``config.base_url`` points at any compatible endpoint.  That is exactly
the ``linteum_agent`` shape:

* ``openai``            -> ``AsyncOpenAI`` + ``responses`` (default)
* ``openai_compatible`` -> ``AsyncOpenAI`` + custom ``base_url`` + ``chat_completions``
* ``moonshot`` / ``qwen`` / ``zhipu`` / ``siliconflow`` -> same class, other ``base_url``
* ``deepseek``          -> :class:`~linteum_agent.runtime.llm.adapters.deepseek.DeepSeekAdapter` (subclass)

The adapter never publishes and never reads ``AgentState`` (I21): it maps SDK
objects to canonical chunks and nothing more.
"""

from __future__ import annotations

from typing import AsyncIterator, Optional

from .base import BaseAdapter, field, function_of, to_openai_message, to_responses_input
from ..errors import LLMError
from ..canonical import (
    Capabilities,
    FinishDelta,
    LlmChunk,
    LlmRequest,
    MetaDelta,
    ReasoningDelta,
    TextDelta,
    ToolCallArgsDelta,
    ToolCallEnd,
    ToolCallStart,
    Usage,
    UsageDelta,
)

#: Responses-API streaming event types that carry reasoning text.
RESPONSES_REASONING_DELTAS = frozenset({
    "response.reasoning_text.delta",
    "response.reasoning_summary_text.delta",
    "response.thinking_text.delta",
})

_CHAT_FINISH = {
    "stop": "stop", "length": "length", "tool_calls": "tool_calls",
    "function_call": "tool_calls", "content_filter": "content_filter",
}
_RESPONSES_FINISH = {
    "completed": "stop", "incomplete": "length", "failed": "error",
    "cancelled": "error",
}


def _reasoning_from(obj) -> str:
    value = field(obj, "reasoning_content") or field(obj, "reasoning")
    return value if isinstance(value, str) else ""


def _usage_from_chat(usage: Optional[object]) -> Usage:
    if usage is None:
        return Usage(0, 0, estimated=True)
    details = field(usage, "prompt_tokens_details")
    return Usage(
        input_tokens=int(field(usage, "prompt_tokens", 0) or 0),
        output_tokens=int(field(usage, "completion_tokens", 0) or 0),
        cache_read=int(field(details, "cached_tokens", 0) or 0),
    )


def _usage_from_responses(usage: Optional[object]) -> Usage:
    if usage is None:
        return Usage(0, 0, estimated=True)
    details = field(usage, "input_tokens_details")
    return Usage(
        input_tokens=int(field(usage, "input_tokens", 0) or 0),
        output_tokens=int(field(usage, "output_tokens", 0) or 0),
        cache_read=int(field(usage, "cache_read_input_tokens", 0)
                       or field(details, "cached_tokens", 0) or 0),
    )


def _tool_calls_from_responses(response) -> list:
    """Responses ``output`` -> ``[(call_id, name, arguments_json)]``."""
    out: list = []
    for item in field(response, "output") or []:
        if field(item, "type") != "function_call":
            continue
        out.append((
            field(item, "call_id", "") or field(item, "id", ""),
            field(item, "name", ""),
            field(item, "arguments", "{}") or "{}",
        ))
    return out


def _reasoning_from_responses(response) -> str:
    parts: list = []
    for item in field(response, "output") or []:
        if field(item, "type") != "reasoning":
            continue
        text = field(item, "reasoning_text")
        if isinstance(text, str) and text:
            parts.append(text)
        for attr in ("content", "summary"):
            for part in field(item, attr) or []:
                part_text = field(part, "text") if not isinstance(part, str) else part
                if isinstance(part_text, str) and part_text:
                    parts.append(part_text)
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------

def _caps_for_mode(mode: str) -> Capabilities:
    params = frozenset({"temperature", "top_p"}) if mode == "responses" else frozenset(
        {"temperature", "top_p", "stop"}
    )
    return Capabilities(
        ctx_window=128_000,
        max_output=16_384,
        tools=True,
        strict_schema=True,
        json_mode=True,
        cache_mode="auto",
        params_supported=params,
        stream_usage=True,
        reasoning=True,
        reasoning_history_policy="strip",
    )


class OpenAIAdapter(BaseAdapter):
    provider = "openai"
    adapter_version = "2.0.0"

    def __init__(
        self,
        config=None,
        *,
        client=None,
        capabilities_map: Optional[dict] = None,
        default_capabilities: Optional[Capabilities] = None,
        pricing: Optional[dict] = None,
    ) -> None:
        self.mode = config.resolved_openai_mode() if config is not None else "chat_completions"
        super().__init__(
            config,
            capabilities_map=capabilities_map,
            default_capabilities=default_capabilities or _caps_for_mode(self.mode),
            pricing=pricing,
            client=client,
        )
        if self.client is None:
            self._build_client()

    def _build_client(self) -> None:
        if self.config is None:
            raise ValueError("OpenAIAdapter requires either a config or an injected client")
        api_key = self.config.resolved_api_key()
        if not api_key:
            env = self.config.defaults.get("api_key_env", "OPENAI_API_KEY")
            raise LLMError(
                f"no API key for provider {self.config.provider!r}: "
                f"pass LLMConfig(api_key=...) or set the {env} environment variable"
            )
        from openai import AsyncOpenAI

        kwargs: dict = {"api_key": api_key, "timeout": self.config.timeout}
        if self.config.resolved_base_url():
            kwargs["base_url"] = self.config.resolved_base_url()
        self.client = AsyncOpenAI(**kwargs)

    # -- request ------------------------------------------------------------
    @staticmethod
    def _responses_tool(tool: dict) -> dict:
        fn = function_of(tool)
        return {
            "type": "function",
            "name": fn["name"],
            "description": fn.get("description", ""),
            "parameters": fn.get("parameters", {"type": "object", "properties": {}}),
        }

    # -- usage mapping (overridable per provider) ---------------------------
    def _chat_usage(self, usage) -> Usage:
        return _usage_from_chat(usage)

    def _responses_usage(self, usage) -> Usage:
        return _usage_from_responses(usage)

    def build_body(self, req: LlmRequest) -> tuple:
        cap = self.capabilities(req.model)
        if self.mode == "responses":
            return self._responses_body(req, cap)
        return self._chat_body(req, cap)

    def _chat_body(self, req: LlmRequest, cap: Capabilities) -> tuple:
        body: dict = {
            "model": self._model_id(req.model),
            "messages": [to_openai_message(m) for m in req.messages],
            "stream": req.stream,
        }
        max_tokens = self._max_tokens(req)          # request value, else config default
        if max_tokens:
            body["max_tokens"] = max_tokens
        if req.tools:
            body["tools"] = [{"type": "function", "function": function_of(t)} for t in req.tools]
        if req.tool_choice:
            body["tool_choice"] = req.tool_choice
        if req.response_format:
            body["response_format"] = req.response_format
        if req.stream and cap.stream_usage:
            body["stream_options"] = {"include_usage": True}  # else no usage while streaming
        return body, self._apply_sampling(body, req)

    def _responses_body(self, req: LlmRequest, cap: Capabilities) -> tuple:
        body: dict = {
            "model": self._model_id(req.model),
            "input": [item for m in req.messages for item in to_responses_input(m)],
            "stream": req.stream,
        }
        max_tokens = self._max_tokens(req)          # request value, else config default
        if max_tokens:
            body["max_output_tokens"] = max_tokens
        if req.tools:
            body["tools"] = [self._responses_tool(t) for t in req.tools]
        if req.tool_choice:
            body["tool_choice"] = req.tool_choice
        return body, self._apply_sampling(body, req)

    # -- streaming ----------------------------------------------------------
    async def astream(self, req: LlmRequest) -> AsyncIterator[LlmChunk]:
        body, _ = self.build_body(req)
        if self.mode == "responses":
            gen = self._astream_responses(req, body)
        else:
            gen = self._astream_chat(req, body)
        async for chunk in gen:
            yield chunk

    async def _astream_chat(self, req: LlmRequest, body: dict) -> AsyncIterator[LlmChunk]:
        stream = await self.client.chat.completions.create(**body)
        emitted_model = False
        async for chunk in stream:
            model = field(chunk, "model")
            if model and not emitted_model:
                yield MetaDelta(model_actual=model, response_id=field(chunk, "id"))
                emitted_model = True
            choices = field(chunk, "choices") or []
            for choice in choices:
                delta = field(choice, "delta")
                if delta is None:
                    continue
                content = field(delta, "content")
                if content:
                    yield TextDelta(content)
                reasoning = _reasoning_from(delta)
                if reasoning:
                    yield ReasoningDelta(reasoning)
                for call in field(delta, "tool_calls") or []:
                    index = field(call, "index", 0) or 0
                    fn = field(call, "function")
                    call_id = field(call, "id")
                    name = field(fn, "name")
                    if call_id or name:
                        yield ToolCallStart(
                            index,
                            call_id or self._synth_id(req.model, index, req.request_id),
                            name or "",
                        )
                    args = field(fn, "arguments")
                    if args:
                        yield ToolCallArgsDelta(index, args)
                finish = field(choice, "finish_reason")
                if finish:
                    yield ToolCallEnd(-1)  # finalise every open call
                    yield FinishDelta(_CHAT_FINISH.get(finish, "stop"), self._chat_usage(field(chunk, "usage")))
            usage = field(chunk, "usage")
            if usage is not None and not choices:
                yield UsageDelta(self._chat_usage(usage))  # trailing usage-only chunk

    async def _astream_responses(self, req: LlmRequest, body: dict) -> AsyncIterator[LlmChunk]:
        stream = await self.client.responses.create(**body)
        final = None
        saw_reasoning = False
        async for event in stream:
            event_type = field(event, "type", "")
            if event_type == "response.output_text.delta":
                delta = field(event, "delta", "") or ""
                if delta:
                    yield TextDelta(delta)
            elif event_type in RESPONSES_REASONING_DELTAS:
                delta = field(event, "delta", "") or ""
                if delta:
                    saw_reasoning = True
                    yield ReasoningDelta(delta)
            elif event_type == "response.completed":
                final = field(event, "response")
        if final is None:
            return
        model = field(final, "model")
        if model:
            yield MetaDelta(model_actual=model, response_id=field(final, "id"))
        if not saw_reasoning:  # providers that only surface reasoning at completion
            reasoning = _reasoning_from_responses(final)
            if reasoning:
                yield ReasoningDelta(reasoning)
        calls = _tool_calls_from_responses(final)
        for index, (call_id, name, arguments_json) in enumerate(calls):
            yield ToolCallStart(index, call_id or self._synth_id(req.model, index, req.request_id), name)
            yield ToolCallArgsDelta(index, arguments_json)
            yield ToolCallEnd(index)
        status = field(final, "status") or "completed"
        finish = "tool_calls" if calls else _RESPONSES_FINISH.get(status, "stop")
        yield FinishDelta(finish, self._responses_usage(field(final, "usage")))


class OpenAICompatibleAdapter(OpenAIAdapter):
    """OpenAI-compatible endpoints (DeepSeek/Moonshot/Qwen/…); requires ``base_url``.

    Behaviourally identical to :class:`OpenAIAdapter` — the only difference is the
    provider label and the requirement that ``base_url`` be configured.
    """

    provider = "openai_compatible"

    def __init__(self, config=None, *, client=None, **kw) -> None:
        if config is not None and not config.resolved_base_url():
            raise ValueError("base_url is required when provider='openai_compatible'")
        super().__init__(config, client=client, **kw)


