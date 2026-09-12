from __future__ import annotations

import threading
from collections import defaultdict
from typing import Any, AsyncIterator, Iterator

try:
    from .config import LLMConfig
    from .llm import (
        AnthropicProvider,
        BaseLLMProvider,
        LLMResponse,
        LLMStreamChunk,
        MessageInputs,
        OpenAICompatibleProvider,
        OpenAIProvider,
    )
except ImportError:  # 兼容旧的直接导入方式
    from config import LLMConfig
    from llm import (
        AnthropicProvider,
        BaseLLMProvider,
        LLMResponse,
        LLMStreamChunk,
        MessageInputs,
        OpenAICompatibleProvider,
        OpenAIProvider,
    )


def _empty_usage() -> dict[str, int]:
    return {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_hit_tokens": 0,
        "total_tokens": 0,
    }


class LLMClient:
    """统一 LLM 门面，并按 session 累计 token usage。"""

    def __init__(self, config: LLMConfig):
        self.config = config
        self.provider = self._create_provider(config)
        self._session_usage: dict[str, dict[str, int]] = defaultdict(_empty_usage)
        self._usage_lock = threading.RLock()

    @staticmethod
    def _create_provider(config: LLMConfig) -> BaseLLMProvider:
        if config.provider == "openai":
            return OpenAIProvider(config)
        if config.provider == "openai_compatible":
            return OpenAICompatibleProvider(config)
        if config.provider == "anthropic":
            return AnthropicProvider(config)
        raise ValueError(f"不支持的 provider: {config.provider}")

    def _record_usage(self, session_id: str, usage: dict[str, int]) -> None:
        with self._usage_lock:
            current = self._session_usage[session_id]
            for key in ("input_tokens", "output_tokens", "cache_hit_tokens"):
                current[key] += int(usage.get(key, 0) or 0)
            current["total_tokens"] += int(
                usage.get("input_tokens", 0) or 0
            ) + int(usage.get("output_tokens", 0) or 0)

    def get_session_usage(self, session_id: str = "default") -> dict[str, int]:
        with self._usage_lock:
            return dict(self._session_usage[session_id])

    def reset_session(self, session_id: str = "default") -> None:
        with self._usage_lock:
            self._session_usage.pop(session_id, None)

    def reset_all_sessions(self) -> None:
        with self._usage_lock:
            self._session_usage.clear()

    def complete(
        self,
        messages: MessageInputs,
        *,
        session_id: str = "default",
        **kwargs: Any,
    ) -> LLMResponse:
        response = self.provider.complete(messages, **kwargs)
        self._record_usage(session_id, response.usage)
        return response

    def stream(
        self,
        messages: MessageInputs,
        *,
        session_id: str = "default",
        **kwargs: Any,
    ) -> Iterator[LLMStreamChunk]:
        for chunk in self.provider.stream(messages, **kwargs):
            if chunk.done:
                usage = chunk.response.usage if chunk.response else chunk.usage
                self._record_usage(session_id, usage)
            yield chunk

    async def acomplete(
        self,
        messages: MessageInputs,
        *,
        session_id: str = "default",
        **kwargs: Any,
    ) -> LLMResponse:
        response = await self.provider.acomplete(messages, **kwargs)
        self._record_usage(session_id, response.usage)
        return response

    async def astream(
        self,
        messages: MessageInputs,
        *,
        session_id: str = "default",
        **kwargs: Any,
    ) -> AsyncIterator[LLMStreamChunk]:
        async for chunk in self.provider.astream(messages, **kwargs):
            if chunk.done:
                usage = chunk.response.usage if chunk.response else chunk.usage
                self._record_usage(session_id, usage)
            yield chunk