from __future__ import annotations

import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Iterator, Literal

from anthropic import Anthropic, AsyncAnthropic
from openai import AsyncOpenAI, OpenAI

try:
    from .config import LLMConfig, OpenAIMode, ProviderName
    from ..messages.agent_message import (
        Message,
        MessageInputs,
        MessageInput,
        ToolCall,
        normalize_messages,
        to_anthropic_message,
        to_openai_message,
        to_responses_input,
    )
except ImportError:  # compatibility with the old style of using the script directory as the import path
    from config import LLMConfig, OpenAIMode, ProviderName
    from ..messages.agent_message import Message, MessageInputs, MessageInput, ToolCall, normalize_messages, to_anthropic_message, to_openai_message, to_responses_input


@dataclass
class LLMResponse:
    text: str
    provider: str
    model: str
    usage: dict[str, int] = field(default_factory=dict)
    raw_response: Any = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    # Chain-of-thought content (DeepSeek reasoning_content / Responses API reasoning item).
    reasoning_content: str = ""


@dataclass
class LLMStreamChunk:
    """
    A unified streaming response.

    Plain delta:
        LLMStreamChunk(delta="hello")

    Reasoning delta:
        LLMStreamChunk(reasoning_content="let me think...")

    Stream end:
        LLMStreamChunk(
            delta="",
            done=True,
            usage={...},
            response=LLMResponse(...)
        )
    """

    delta: str = ""
    done: bool = False
    usage: dict[str, int] = field(default_factory=dict)
    response: LLMResponse | None = None
    # The chain-of-thought text in this delta.
    reasoning_content: str = ""


def normalize_usage(
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_hit_tokens: int = 0,
) -> dict[str, int]:
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_hit_tokens": cache_hit_tokens,
        "total_tokens": input_tokens + output_tokens,
    }


def _nested_usage_value(usage: Any, *names: str) -> int:
    """Read a numeric value from the provider usage that may be nested inside a details object."""
    for name in names:
        value = getattr(usage, name, None)
        if value is not None:
            return int(value or 0)
        if isinstance(usage, dict) and name in usage:
            return int(usage[name] or 0)
    return 0


def _field(value: Any, name: str, default: Any = None) -> Any:
    """Support both object and dict access (both old and new OpenAI SDK versions may return a dict)."""
    if value is None:
        return default
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def _reasoning_from_message(message: Any) -> str:
    """Extract the chain-of-thought text from a Chat Completions assistant message."""
    reasoning = _field(message, "reasoning_content") or _field(message, "reasoning")
    return reasoning if isinstance(reasoning, str) else ""


_RESPONSES_REASONING_PART_TYPES = {"reasoning_text", "summary_text", "thinking_text"}


def _reasoning_from_responses(response: Any) -> str:
    """Extract the text of reasoning items from the Responses API output."""
    parts: list[str] = []

    for item in _field(response, "output") or []:
        if _field(item, "type") != "reasoning":
            continue

        text = _field(item, "reasoning_text")
        if isinstance(text, str) and text:
            parts.append(text)

        for attribute in ("content", "summary"):
            for part in _field(item, attribute) or []:
                if isinstance(part, str):
                    if part:
                        parts.append(part)
                    continue
                part_type = _field(part, "type")
                part_text = _field(part, "text")
                if part_text and (
                    part_type is None
                    or part_type in _RESPONSES_REASONING_PART_TYPES
                ):
                    parts.append(part_text)

    return "\n".join(parts)


def _accumulate_tool_calls(
    state: dict[int, dict[str, str]],
    deltas: Any,
) -> None:
    """Accumulate the tool_calls fragments in a streaming delta by index."""
    for delta in deltas or []:
        index = _field(delta, "index")
        if index is None:
            index = len(state)
        entry = state.setdefault(index, {"id": "", "name": "", "arguments": ""})

        call_id = _field(delta, "id")
        if call_id:
            entry["id"] = call_id

        function = _field(delta, "function")
        if function is None:
            continue

        name = _field(function, "name")
        if name:
            entry["name"] = name

        arguments = _field(function, "arguments")
        if arguments:
            entry["arguments"] += arguments


def _tool_calls_from_state(state: dict[int, dict[str, str]]) -> list[ToolCall]:
    return [
        ToolCall(
            id=entry["id"],
            name=entry["name"],
            arguments=entry["arguments"] or "{}",
        )
        for _, entry in sorted(state.items())
    ]


def build_request(
    *,
    model: str,
    messages: list[MessageInput],
    temperature: float | None,
    max_tokens: int | None,
    top_p: float | None,
    **kwargs: Any,
) -> dict[str, Any]:
    request: dict[str, Any] = {
        "model": model,
        "messages": [to_openai_message(message) for message in normalize_messages(messages)],
    }

    if temperature is not None:
        request["temperature"] = temperature

    if max_tokens is not None:
        request["max_tokens"] = max_tokens

    if top_p is not None:
        request["top_p"] = top_p

    request.update(kwargs)
    if "tools" in request:
        request["tools"] = [tool.to_openai() if hasattr(tool, "to_openai") else tool for tool in request["tools"]]
    return request


def _tool_calls_from_openai(message: Any) -> list[ToolCall]:
    return [
        ToolCall(
            id=_field(call, "id", ""),
            name=_field(_field(call, "function"), "name", ""),
            arguments=_field(_field(call, "function"), "arguments", "{}"),
        )
        for call in (_field(message, "tool_calls") or [])
    ]


def _tool_calls_from_responses(response: Any) -> list[ToolCall]:
    return [
        ToolCall(
            id=_field(item, "call_id") or _field(item, "id", ""),
            name=_field(item, "name", ""),
            arguments=_field(item, "arguments", "{}"),
        )
        for item in (_field(response, "output") or [])
        if _field(item, "type", "") == "function_call"
    ]


def _tool_calls_from_anthropic(response: Any) -> list[ToolCall]:
    return [
        ToolCall(
            id=getattr(block, "id", ""),
            name=getattr(block, "name", ""),
            arguments=getattr(block, "input", {}) or {},
        )
        for block in (getattr(response, "content", None) or [])
        if getattr(block, "type", "") == "tool_use"
    ]


class BaseLLMProvider(ABC):
    """
    The unified interface that all model providers must implement.
    """

    def __init__(self, config: LLMConfig):
        self.config = config

    @abstractmethod
    def complete(
        self,
        messages: MessageInputs,
        **kwargs: Any,
    ) -> LLMResponse:
        pass

    @abstractmethod
    def stream(
        self,
        messages: list[Message],
        **kwargs: Any,
    ) -> Iterator[LLMStreamChunk]:
        pass

    @abstractmethod
    async def acomplete(
        self,
        messages: list[Message],
        **kwargs: Any,
    ) -> LLMResponse:
        pass

    @abstractmethod
    async def astream(
        self,
        messages: list[Message],
        **kwargs: Any,
    ) -> AsyncIterator[LLMStreamChunk]:
        pass

class OpenAIProvider(BaseLLMProvider):
    """
    The OpenAI model provider.

    Supports:
    - Responses API
    - Chat Completions API
    - synchronous calls
    - asynchronous calls
    - synchronous streaming calls
    - asynchronous streaming calls
    """

    provider_name = "openai"

    def __init__(self, config: LLMConfig):
        super().__init__(config)

        api_key = config.api_key or os.getenv("OPENAI_API_KEY")

        if not api_key:
            raise ValueError(
                "OpenAI API Key not found; configure it via api_key "
                "or the OPENAI_API_KEY environment variable"
            )

        self.client = OpenAI(
            api_key=api_key,
            base_url=config.base_url,
            timeout=config.timeout,
        )

        self.async_client = AsyncOpenAI(
            api_key=api_key,
            base_url=config.base_url,
            timeout=config.timeout,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def complete(
        self,
        messages: list[Message],
        **kwargs: Any,
    ) -> LLMResponse:
        messages = normalize_messages(messages)
        self._validate_messages(messages)

        if self.config.openai_mode == "responses":
            return self._complete_responses(messages, **kwargs)

        return self._complete_chat_completions(messages, **kwargs)

    def stream(
        self,
        messages: MessageInputs,
        **kwargs: Any,
    ) -> Iterator[LLMStreamChunk]:
        messages = normalize_messages(messages)
        self._validate_messages(messages)

        if self.config.openai_mode == "responses":
            yield from self._stream_responses(messages, **kwargs)
            return

        yield from self._stream_chat_completions(messages, **kwargs)

    async def acomplete(
        self,
        messages: MessageInputs,
        **kwargs: Any,
    ) -> LLMResponse:
        messages = normalize_messages(messages)
        self._validate_messages(messages)

        if self.config.openai_mode == "responses":
            return await self._acomplete_responses(messages, **kwargs)

        return await self._acomplete_chat_completions(messages, **kwargs)

    async def astream(
        self,
        messages: MessageInputs,
        **kwargs: Any,
    ) -> AsyncIterator[LLMStreamChunk]:
        messages = normalize_messages(messages)
        self._validate_messages(messages)

        if self.config.openai_mode == "responses":
            async for chunk in self._astream_responses(
                messages,
                **kwargs,
            ):
                yield chunk
            return

        async for chunk in self._astream_chat_completions(
            messages,
            **kwargs,
        ):
            yield chunk

    # ------------------------------------------------------------------
    # Common helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_messages(messages: list[Message]) -> None:
        if not messages:
            raise ValueError("messages must not be empty")

        for message in messages:
            if not getattr(message, "role", None):
                raise ValueError("every message must contain a role")

    def _responses_request(
        self,
        messages: list[Message],
        *,
        stream: bool = False,
        **kwargs: Any,
    ) -> dict[str, Any]:
        request: dict[str, Any] = {
            "model": self.config.model,
            "input": [
                item
                for message in messages
                for item in to_responses_input(message)
            ],
            "stream": stream,
        }

        max_output_tokens = kwargs.pop(
            "max_output_tokens",
            self.config.max_tokens,
        )

        if max_output_tokens is not None:
            request["max_output_tokens"] = max_output_tokens

        temperature = kwargs.pop(
            "temperature",
            self.config.temperature,
        )

        if temperature is not None:
            request["temperature"] = temperature

        top_p = kwargs.pop(
            "top_p",
            self.config.top_p,
        )

        if top_p is not None:
            request["top_p"] = top_p

        request.update(kwargs)
        if "tools" in request:
            request["tools"] = [
                tool.to_responses() if hasattr(tool, "to_responses")
                else tool
                for tool in request["tools"]
            ]

        return request

    def _chat_request(
        self,
        messages: list[Message],
        *,
        stream: bool = False,
        **kwargs: Any,
    ) -> dict[str, Any]:
        return build_request(
            model=self.config.model,
            messages=messages,
            temperature=kwargs.pop(
                "temperature",
                self.config.temperature,
            ),
            max_tokens=kwargs.pop(
                "max_tokens",
                self.config.max_tokens,
            ),
            top_p=kwargs.pop(
                "top_p",
                self.config.top_p,
            ),
            stream=stream,
            **kwargs,
        )

    def _response_usage(
        self,
        response: Any,
    ) -> dict[str, int]:
        usage = getattr(response, "usage", None)

        if usage is None:
            return {}

        return normalize_usage(
            input_tokens=getattr(usage, "input_tokens", 0),
            output_tokens=getattr(usage, "output_tokens", 0),
            cache_hit_tokens=_nested_usage_value(
                usage,
                "cache_read_input_tokens",
                "cached_tokens",
            ) or _nested_usage_value(
                getattr(usage, "input_tokens_details", None),
                "cached_tokens",
            ),
        )

    def _chat_usage(
        self,
        response: Any,
    ) -> dict[str, int]:
        usage = getattr(response, "usage", None)

        if usage is None:
            return {}

        return normalize_usage(
            input_tokens=getattr(usage, "prompt_tokens", 0),
            output_tokens=getattr(usage, "completion_tokens", 0),
            cache_hit_tokens=_nested_usage_value(
                getattr(usage, "prompt_tokens_details", None),
                "cached_tokens",
            ),
        )

    def _new_response(
        self,
        *,
        text: str,
        model: str | None = None,
        usage: dict[str, int] | None = None,
        raw_response: Any = None,
        tool_calls: list[ToolCall] | None = None,
        reasoning_content: str = "",
    ) -> LLMResponse:
        return LLMResponse(
            text=text,
            provider=self.provider_name,
            model=model or self.config.model,
            usage=usage or {},
            raw_response=raw_response,
            tool_calls=tool_calls or [],
            reasoning_content=reasoning_content or "",
        )

    # ------------------------------------------------------------------
    # Responses API - synchronous
    # ------------------------------------------------------------------

    def _complete_responses(
        self,
        messages: list[Message],
        **kwargs: Any,
    ) -> LLMResponse:
        request = self._responses_request(
            messages,
            stream=False,
            **kwargs,
        )

        response = self.client.responses.create(**request)

        return self._new_response(
            text=getattr(response, "output_text", "") or "",
            model=getattr(response, "model", None),
            usage=self._response_usage(response),
            tool_calls=_tool_calls_from_responses(response),
            reasoning_content=_reasoning_from_responses(response),
            raw_response=response,
        )

    def _stream_responses(
        self,
        messages: list[Message],
        **kwargs: Any,
    ) -> Iterator[LLMStreamChunk]:
        request = self._responses_request(
            messages,
            stream=True,
            **kwargs,
        )

        stream = self.client.responses.create(**request)

        text_parts: list[str] = []
        reasoning_parts: list[str] = []
        final_response: Any = None

        for event in stream:
            event_type = getattr(event, "type", "")

            if event_type == "response.output_text.delta":
                delta = getattr(event, "delta", "") or ""

                if delta:
                    text_parts.append(delta)
                    yield LLMStreamChunk(delta=delta)

            elif event_type in (
                "response.reasoning_text.delta",
                "response.reasoning_summary_text.delta",
                "response.thinking_text.delta",
            ):
                reasoning_delta = getattr(event, "delta", "") or ""

                if reasoning_delta:
                    reasoning_parts.append(reasoning_delta)
                    yield LLMStreamChunk(reasoning_content=reasoning_delta)

            elif event_type == "response.completed":
                final_response = getattr(event, "response", None)

        usage = self._response_usage(final_response)

        response = self._new_response(
            text="".join(text_parts),
            model=getattr(final_response, "model", None),
            usage=usage,
            tool_calls=_tool_calls_from_responses(final_response),
            reasoning_content="".join(reasoning_parts)
            or _reasoning_from_responses(final_response),
            raw_response=final_response,
        )

        yield LLMStreamChunk(
            done=True,
            usage=usage,
            response=response,
        )

    # ------------------------------------------------------------------
    # Chat Completions API - synchronous
    # ------------------------------------------------------------------

    def _complete_chat_completions(
        self,
        messages: list[Message],
        **kwargs: Any,
    ) -> LLMResponse:
        request = self._chat_request(
            messages,
            stream=False,
            **kwargs,
        )

        response = self.client.chat.completions.create(**request)

        if not response.choices:
            raise RuntimeError("Chat Completions response contains no choices")

        content = response.choices[0].message.content or ""
        message = response.choices[0].message

        return self._new_response(
            text=content,
            model=getattr(response, "model", None),
            usage=self._chat_usage(response),
            tool_calls=_tool_calls_from_openai(message),
            reasoning_content=_reasoning_from_message(message),
            raw_response=response,
        )

    def _stream_chat_completions(
        self,
        messages: list[Message],
        **kwargs: Any,
    ) -> Iterator[LLMStreamChunk]:
        request = self._chat_request(
            messages,
            stream=True,
            **kwargs,
        )

        stream = self.client.chat.completions.create(**request)

        text_parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_call_state: dict[int, dict[str, str]] = {}
        usage: dict[str, int] = {}

        for chunk in stream:
            if getattr(chunk, "choices", None):
                delta = chunk.choices[0].delta

                content = getattr(delta, "content", None) or ""

                if content:
                    text_parts.append(content)
                    yield LLMStreamChunk(delta=content)

                reasoning = _reasoning_from_message(delta)

                if reasoning:
                    reasoning_parts.append(reasoning)
                    yield LLMStreamChunk(reasoning_content=reasoning)

                _accumulate_tool_calls(
                    tool_call_state,
                    _field(delta, "tool_calls"),
                )

            chunk_usage = getattr(chunk, "usage", None)

            if chunk_usage is not None:
                usage = normalize_usage(
                    input_tokens=getattr(
                        chunk_usage,
                        "prompt_tokens",
                        0,
                    ),
                    output_tokens=getattr(
                        chunk_usage,
                        "completion_tokens",
                        0,
                    ),
                    cache_hit_tokens=_nested_usage_value(
                        getattr(chunk_usage, "prompt_tokens_details", None),
                        "cached_tokens",
                    ),
                )

        response = self._new_response(
            text="".join(text_parts),
            model=self.config.model,
            usage=usage,
            tool_calls=_tool_calls_from_state(tool_call_state),
            reasoning_content="".join(reasoning_parts),
        )

        yield LLMStreamChunk(
            done=True,
            usage=usage,
            response=response,
        )

    # ------------------------------------------------------------------
    # Responses API - asynchronous
    # ------------------------------------------------------------------

    async def _acomplete_responses(
        self,
        messages: list[Message],
        **kwargs: Any,
    ) -> LLMResponse:
        request = self._responses_request(
            messages,
            stream=False,
            **kwargs,
        )

        response = await self.async_client.responses.create(**request)

        return self._new_response(
            text=getattr(response, "output_text", "") or "",
            model=getattr(response, "model", None),
            usage=self._response_usage(response),
            raw_response=response,
        )

    async def _astream_responses(
        self,
        messages: list[Message],
        **kwargs: Any,
    ) -> AsyncIterator[LLMStreamChunk]:
        request = self._responses_request(
            messages,
            stream=True,
            **kwargs,
        )

        stream = await self.async_client.responses.create(**request)

        text_parts: list[str] = []
        reasoning_parts: list[str] = []
        final_response: Any = None

        async for event in stream:
            event_type = getattr(event, "type", "")

            if event_type == "response.output_text.delta":
                delta = getattr(event, "delta", "") or ""

                if delta:
                    text_parts.append(delta)
                    yield LLMStreamChunk(delta=delta)

            elif event_type in (
                "response.reasoning_text.delta",
                "response.reasoning_summary_text.delta",
                "response.thinking_text.delta",
            ):
                reasoning_delta = getattr(event, "delta", "") or ""

                if reasoning_delta:
                    reasoning_parts.append(reasoning_delta)
                    yield LLMStreamChunk(reasoning_content=reasoning_delta)

            elif event_type == "response.completed":
                final_response = getattr(event, "response", None)

        usage = self._response_usage(final_response)

        response = self._new_response(
            text="".join(text_parts),
            model=getattr(final_response, "model", None),
            usage=usage,
            tool_calls=_tool_calls_from_responses(final_response),
            reasoning_content="".join(reasoning_parts)
            or _reasoning_from_responses(final_response),
            raw_response=final_response,
        )

        yield LLMStreamChunk(
            done=True,
            usage=usage,
            response=response,
        )

    # ------------------------------------------------------------------
    # Chat Completions API - asynchronous
    # ------------------------------------------------------------------

    async def _acomplete_chat_completions(
        self,
        messages: list[Message],
        **kwargs: Any,
    ) -> LLMResponse:
        request = self._chat_request(
            messages,
            stream=False,
            **kwargs,
        )

        response = await self.async_client.chat.completions.create(**request)

        if not response.choices:
            raise RuntimeError("Chat Completions response contains no choices")

        content = response.choices[0].message.content or ""
        message = response.choices[0].message

        return self._new_response(
            text=content,
            model=getattr(response, "model", None),
            usage=self._chat_usage(response),
            tool_calls=_tool_calls_from_openai(message),
            reasoning_content=_reasoning_from_message(message),
            raw_response=response,
        )

    async def _astream_chat_completions(
        self,
        messages: list[Message],
        **kwargs: Any,
    ) -> AsyncIterator[LLMStreamChunk]:
        request = self._chat_request(
            messages,
            stream=True,
            **kwargs,
        )

        stream = await self.async_client.chat.completions.create(**request)

        text_parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_call_state: dict[int, dict[str, str]] = {}
        usage: dict[str, int] = {}

        async for chunk in stream:
            if getattr(chunk, "choices", None):
                delta = chunk.choices[0].delta

                content = getattr(delta, "content", None) or ""

                if content:
                    text_parts.append(content)
                    yield LLMStreamChunk(delta=content)

                reasoning = _reasoning_from_message(delta)

                if reasoning:
                    reasoning_parts.append(reasoning)
                    yield LLMStreamChunk(reasoning_content=reasoning)

                _accumulate_tool_calls(
                    tool_call_state,
                    _field(delta, "tool_calls"),
                )

            chunk_usage = getattr(chunk, "usage", None)

            if chunk_usage is not None:
                usage = normalize_usage(
                    input_tokens=getattr(
                        chunk_usage,
                        "prompt_tokens",
                        0,
                    ),
                    output_tokens=getattr(
                        chunk_usage,
                        "completion_tokens",
                        0,
                    ),
                    cache_hit_tokens=_nested_usage_value(
                        getattr(chunk_usage, "prompt_tokens_details", None),
                        "cached_tokens",
                    ),
                )

        response = self._new_response(
            text="".join(text_parts),
            model=self.config.model,
            usage=usage,
            tool_calls=_tool_calls_from_state(tool_call_state),
            reasoning_content="".join(reasoning_parts),
        )

        yield LLMStreamChunk(
            done=True,
            usage=usage,
            response=response,
        )

class OpenAICompatibleProvider(OpenAIProvider):
    """
    OpenAI-compatible models.

    For example:
    - DeepSeek
    - Moonshot
    - SiliconFlow
    - Qwen (Tongyi Qianwen) compatible endpoint
    - Zhipu compatible endpoint
    """
    provider_name = "openai_compatible"

    def __init__(self, config: LLMConfig):
        if not config.base_url:
            raise ValueError(
                "base_url is required when provider='openai_compatible'"
            )

        super().__init__(config)

    def complete(
        self,
        messages: list[Message],
        **kwargs: Any,
    ) -> LLMResponse:
        result = super().complete(messages, **kwargs)
        result.provider = "openai_compatible"
        return result

    def stream(
        self,
        messages: list[Message],
        **kwargs: Any,
    ) -> Iterator[LLMStreamChunk]:
        for chunk in super().stream(messages, **kwargs):
            if chunk.response:
                chunk.response.provider = "openai_compatible"
            yield chunk

    async def acomplete(
        self,
        messages: list[Message],
        **kwargs: Any,
    ) -> LLMResponse:
        result = await super().acomplete(messages, **kwargs)
        result.provider = "openai_compatible"
        return result

    async def astream(
        self,
        messages: list[Message],
        **kwargs: Any,
    ) -> AsyncIterator[LLMStreamChunk]:
        async for chunk in super().astream(messages, **kwargs):
            if chunk.response:
                chunk.response.provider = "openai_compatible"
            yield chunk


class AnthropicProvider(BaseLLMProvider):
    """
    Anthropic Claude Provider。
    """

    def __init__(self, config: LLMConfig):
        super().__init__(config)

        api_key = config.api_key or os.getenv("ANTHROPIC_API_KEY")

        self.client = Anthropic(
            api_key=api_key,
            timeout=config.timeout,
        )

        self.async_client = AsyncAnthropic(
            api_key=api_key,
            timeout=config.timeout,
        )

    def _split_messages(
        self,
        messages: list[Message],
    ) -> tuple[str | None, list[dict[str, str]]]:
        system_parts: list[str] = []
        anthropic_messages: list[dict[str, str]] = []

        for message in normalize_messages(messages):
            if getattr(message, "role", None) == "system":
                system_parts.append(message.content)
                continue
            converted = to_anthropic_message(message)
            if converted:
                anthropic_messages.append(converted)

        system_prompt = "\n".join(system_parts) or None

        return system_prompt, anthropic_messages

    def _build_request(
        self,
        messages: list[Message],
        **kwargs: Any,
    ) -> dict[str, Any]:
        system_prompt, anthropic_messages = self._split_messages(messages)

        request: dict[str, Any] = {
            "model": self.config.model,
            "messages": anthropic_messages,
            "max_tokens": kwargs.pop(
                "max_tokens",
                self.config.max_tokens,
            ),
        }

        if system_prompt:
            request["system"] = system_prompt

        if self.config.temperature is not None:
            request["temperature"] = kwargs.pop(
                "temperature",
                self.config.temperature,
            )

        if self.config.top_p is not None:
            request["top_p"] = kwargs.pop(
                "top_p",
                self.config.top_p,
            )

        request.update(kwargs)
        if "tools" in request:
            request["tools"] = [
                tool.to_anthropic() if hasattr(tool, "to_anthropic") else tool
                for tool in request["tools"]
            ]
        return request

    def complete(
        self,
        messages: list[Message],
        **kwargs: Any,
    ) -> LLMResponse:
        request = self._build_request(messages, **kwargs)
        response = self.client.messages.create(**request)

        text_parts: list[str] = []

        for block in response.content:
            if getattr(block, "type", None) == "text":
                text_parts.append(block.text)

        usage = normalize_usage(
            input_tokens=getattr(response.usage, "input_tokens", 0),
            output_tokens=getattr(response.usage, "output_tokens", 0),
            cache_hit_tokens=_nested_usage_value(
                response.usage,
                "cache_read_input_tokens",
            ),
        )

        return LLMResponse(
            text="".join(text_parts),
            provider="anthropic",
            model=getattr(response, "model", None) or self.config.model,
            usage=usage,
            raw_response=response,
            tool_calls=_tool_calls_from_anthropic(response),
        )

    def stream(
        self,
        messages: list[Message],
        **kwargs: Any,
    ) -> Iterator[LLMStreamChunk]:
        request = self._build_request(messages, **kwargs)

        text_parts: list[str] = []

        with self.client.messages.stream(**request) as stream:
            for text in stream.text_stream:
                text_parts.append(text)
                yield LLMStreamChunk(delta=text)

            final_response = stream.get_final_message()

        usage = normalize_usage(
            input_tokens=getattr(final_response.usage, "input_tokens", 0),
            output_tokens=getattr(final_response.usage, "output_tokens", 0),
            cache_hit_tokens=_nested_usage_value(
                final_response.usage,
                "cache_read_input_tokens",
            ),
        )

        response = LLMResponse(
            text="".join(text_parts),
            provider="anthropic",
            model=getattr(
                final_response,
                "model",
                None,
            ) or self.config.model,
            usage=usage,
            raw_response=final_response,
            tool_calls=_tool_calls_from_anthropic(final_response),
        )

        yield LLMStreamChunk(
            done=True,
            usage=usage,
            response=response,
        )

    async def acomplete(
        self,
        messages: list[Message],
        **kwargs: Any,
    ) -> LLMResponse:
        request = self._build_request(messages, **kwargs)
        response = await self.async_client.messages.create(**request)

        text_parts: list[str] = []

        for block in response.content:
            if getattr(block, "type", None) == "text":
                text_parts.append(block.text)

        usage = normalize_usage(
            input_tokens=getattr(response.usage, "input_tokens", 0),
            output_tokens=getattr(response.usage, "output_tokens", 0),
            cache_hit_tokens=_nested_usage_value(
                response.usage,
                "cache_read_input_tokens",
            ),
        )

        return LLMResponse(
            text="".join(text_parts),
            provider="anthropic",
            model=getattr(response, "model", None) or self.config.model,
            usage=usage,
            raw_response=response,
            tool_calls=_tool_calls_from_anthropic(response),
        )

    async def astream(
        self,
        messages: list[Message],
        **kwargs: Any,
    ) -> AsyncIterator[LLMStreamChunk]:
        request = self._build_request(messages, **kwargs)

        text_parts: list[str] = []

        async with self.async_client.messages.stream(**request) as stream:
            async for text in stream.text_stream:
                text_parts.append(text)
                yield LLMStreamChunk(delta=text)

            final_response = await stream.get_final_message()

        usage = normalize_usage(
            input_tokens=getattr(final_response.usage, "input_tokens", 0),
            output_tokens=getattr(final_response.usage, "output_tokens", 0),
            cache_hit_tokens=_nested_usage_value(
                final_response.usage,
                "cache_read_input_tokens",
            ),
        )

        response = LLMResponse(
            text="".join(text_parts),
            provider="anthropic",
            model=getattr(
                final_response,
                "model",
                None,
            ) or self.config.model,
            usage=usage,
            raw_response=final_response,
            tool_calls=_tool_calls_from_anthropic(final_response),
        )

        yield LLMStreamChunk(
            done=True,
            usage=usage,
            response=response,
        )

class DeepseekCompatibleProvider(OpenAIProvider):
    """
    The DeepSeek Provider.

    Supports both ``provider="deepseek"`` and the legacy ``provider="deepseek_compatible"``.

    Default behavior:
    - base_url defaults to ``https://api.deepseek.com``
    - api_key reads ``DEEPSEEK_API_KEY`` by default, falling back to ``OPENAI_API_KEY``
    - supports thinking mode (chain of thought) and, when tools are carried, sends back
      ``reasoning_content`` as DeepSeek requires, enabling multi-turn "chain of thought + tool
      calling" conversations
    """

    provider_name = "deepseek"
    default_base_url = "https://api.deepseek.com"
    api_key_env = "DEEPSEEK_API_KEY"

    def __init__(self, config: LLMConfig):
        api_key = (
            config.api_key
            or os.getenv(self.api_key_env)
            or os.getenv("OPENAI_API_KEY")
        )

        if not api_key:
            raise ValueError(
                "DeepSeek API Key not found; configure it via api_key "
                f"or the {self.api_key_env} environment variable"
            )

        if not config.base_url:
            config.base_url = self.default_base_url

        if not config.api_key:
            config.api_key = api_key

        super().__init__(config)

    # ------------------------------------------------------------------
    # DeepSeek thinking mode parameter injection
    # ------------------------------------------------------------------

    def _thinking_kwargs(
        self,
        kwargs: dict[str, Any],
        *,
        responses: bool,
    ) -> dict[str, Any]:
        """Translate the thinking config from config/call arguments into DeepSeek request parameters."""
        kwargs = dict(kwargs)

        effort = kwargs.pop("reasoning_effort", self.config.reasoning_effort)
        enabled = kwargs.pop("thinking", self.config.thinking)

        if responses:
            # Responses API: reasoning={"effort": "none|low|high|max"}
            if effort is not None or enabled is not None:
                if enabled is False:
                    effort = "none"
                kwargs.setdefault("reasoning", {"effort": effort or "high"})
            return kwargs

        # Chat Completions: reasoning_effort + extra_body.thinking
        if effort is not None:
            kwargs.setdefault("reasoning_effort", effort)

        if enabled is not None:
            extra_body = dict(kwargs.get("extra_body") or {})
            extra_body.setdefault(
                "thinking",
                {"type": "enabled" if enabled else "disabled"},
            )
            kwargs["extra_body"] = extra_body

        return kwargs

    def _chat_request(
        self,
        messages: list[Message],
        *,
        stream: bool = False,
        **kwargs: Any,
    ) -> dict[str, Any]:
        if stream:
            # DeepSeek returns usage in the last chunk only when include_usage is explicitly enabled.
            stream_options = dict(kwargs.get("stream_options") or {})
            stream_options.setdefault("include_usage", True)
            kwargs["stream_options"] = stream_options

        return super()._chat_request(
            messages,
            stream=stream,
            **self._thinking_kwargs(kwargs, responses=False),
        )

    def _responses_request(
        self,
        messages: list[Message],
        *,
        stream: bool = False,
        **kwargs: Any,
    ) -> dict[str, Any]:
        return super()._responses_request(
            messages,
            stream=stream,
            **self._thinking_kwargs(kwargs, responses=True),
        )


def __getattr__(name: str) -> Any:
    """Keep ``from llm import LLMClient`` working without a circular import."""

    if name == "LLMClient":
        try:
            from .client import LLMClient
        except ImportError:
            from client import LLMClient

        return LLMClient

    raise AttributeError(name)