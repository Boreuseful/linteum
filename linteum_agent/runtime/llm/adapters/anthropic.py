"""Anthropic adapter (§38.2) via the ``anthropic`` SDK.

Structurally different from the OpenAI family:

* ``system`` is a **top-level parameter**, not a message;
* tool results are ``tool_result`` blocks on a ``user`` turn;
* tool calls are ``tool_use`` blocks; ``max_tokens`` is **required**;
* ``thinking`` blocks carry a **signature** and, with tools, must be kept.

Like ``linteum_agent``, streaming uses ``client.messages.stream(...)`` with
``text_stream`` and ``await get_final_message()``; reasoning/tool-use blocks are
therefore emitted from the final message (the SDK's ``text_stream`` yields text
only).
"""

from __future__ import annotations

import json
from typing import AsyncIterator, Optional

from .base import BaseAdapter, field, function_of, to_anthropic_message
from ..errors import LLMError
from ..canonical import (
    Capabilities,
    FinishDelta,
    LlmChunk,
    LlmRequest,
    MetaDelta,
    ReasoningDelta,
    ReasoningSignature,
    TextDelta,
    ToolCallArgsDelta,
    ToolCallEnd,
    ToolCallStart,
    Usage,
)

_ANTHROPIC_FINISH = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "max_tokens": "length",
    "tool_use": "tool_calls",
    "refusal": "content_filter",
}


def _anthropic_default_caps() -> Capabilities:
    return Capabilities(
        ctx_window=200_000,
        max_output=8_192,
        tools=True,
        json_mode=False,
        cache_mode="explicit",
        max_cache_breakpoints=4,
        reasoning=True,
        stream_usage=True,
        # extended thinking + tools requires keeping the reasoning history
        reasoning_history_policy="keep",
        params_supported=frozenset({"temperature", "top_p", "stop"}),
    )


def _usage_from_anthropic(usage) -> Usage:
    if usage is None:
        return Usage(0, 0, estimated=True)
    return Usage(
        input_tokens=int(field(usage, "input_tokens", 0) or 0),
        output_tokens=int(field(usage, "output_tokens", 0) or 0),
        cache_read=int(field(usage, "cache_read_input_tokens", 0) or 0),
        cache_write=int(field(usage, "cache_creation_input_tokens", 0) or 0),
    )


class AnthropicAdapter(BaseAdapter):
    provider = "anthropic"
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
        super().__init__(
            config,
            capabilities_map=capabilities_map,
            default_capabilities=default_capabilities or _anthropic_default_caps(),
            pricing=pricing,
            client=client,
        )
        if self.client is None:
            self._build_client()

    def _build_client(self) -> None:
        if self.config is None:
            raise ValueError("AnthropicAdapter requires either a config or an injected client")
        api_key = self.config.resolved_api_key()
        if not api_key:
            env = self.config.defaults.get("api_key_env", "ANTHROPIC_API_KEY")
            raise LLMError(
                f"no API key for provider {self.config.provider!r}: "
                f"pass LLMConfig(api_key=...) or set the {env} environment variable"
            )
        from anthropic import AsyncAnthropic

        self.client = AsyncAnthropic(api_key=api_key, timeout=self.config.timeout)

    # -- request ------------------------------------------------------------
    def build_body(self, req: LlmRequest) -> tuple:
        system_parts = [m.text for m in req.messages if m.role == "system"]
        messages = [
            converted
            for m in req.messages
            for converted in [to_anthropic_message(m)]
            if converted is not None
        ]
        body: dict = {
            "model": self._model_id(req.model),
            "messages": messages,
            "max_tokens": self._max_tokens(req) or self.capabilities(req.model).max_output,  # ★ required
            "stream": req.stream,
        }
        if system_parts:
            body["system"] = "\n".join(system_parts)
        if req.tools:
            tools = []
            for t in req.tools:
                fn = function_of(t)          # accept wrapped or bare function schema
                tools.append({
                    "name": fn["name"],
                    "description": fn.get("description", ""),
                    "input_schema": fn.get("parameters", {"type": "object", "properties": {}}),
                })
            body["tools"] = tools
        if req.tool_choice:
            body["tool_choice"] = req.tool_choice
        return body, self._apply_sampling(body, req)

    # -- streaming ----------------------------------------------------------
    async def astream(self, req: LlmRequest) -> AsyncIterator[LlmChunk]:
        body, _ = self.build_body(req)
        async with self.client.messages.stream(**body) as stream:
            async for text in stream.text_stream:
                if text:
                    yield TextDelta(text)
            final = await stream.get_final_message()

        model = field(final, "model")
        if model:
            yield MetaDelta(model_actual=model, response_id=field(final, "id"))

        blocks = field(final, "content") or []
        for block in blocks:
            if field(block, "type") == "thinking":
                thinking = field(block, "thinking", "") or ""
                if thinking:
                    yield ReasoningDelta(thinking)
                signature = field(block, "signature")
                if signature:
                    yield ReasoningSignature(signature)

        calls = [b for b in blocks if field(b, "type") == "tool_use"]
        for index, block in enumerate(calls):
            yield ToolCallStart(index, field(block, "id", ""), field(block, "name", ""))
            yield ToolCallArgsDelta(
                index, json.dumps(field(block, "input", {}) or {}, ensure_ascii=False)
            )
            yield ToolCallEnd(index)

        stop = field(final, "stop_reason") or ""
        finish = "tool_calls" if calls else _ANTHROPIC_FINISH.get(stop, "stop")
        yield FinishDelta(finish, _usage_from_anthropic(field(final, "usage")))
