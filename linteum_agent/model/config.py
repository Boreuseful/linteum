from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


ProviderName = Literal[
    "openai",
    "openai_compatible",
    "anthropic",
    "deepseek",
    "deepseek_compatible",
]

OpenAIMode = Literal[
    "responses",
    "chat_completions",
]

# DeepSeek 思维链（thinking mode）的推理强度。
ReasoningEffort = Literal[
    "none",
    "low",
    "high",
    "max",
]


@dataclass
class LLMConfig:
    """LLM provider and default request configuration."""

    provider: ProviderName
    model: str

    # OpenAI and OpenAI-compatible models
    openai_mode: OpenAIMode = "responses"

    api_key: str | None = None
    base_url: str | None = None
    timeout: float = 60.0

    # Default request parameters
    temperature: float | None = 0.7
    max_tokens: int = 1024
    top_p: float | None = None

    # 思维链（reasoning / thinking mode）配置。
    # DeepSeek 在 Chat Completions 下映射到 reasoning_effort + extra_body.thinking，
    # 在 Responses API 下映射到 reasoning.effort。
    reasoning_effort: ReasoningEffort | str | None = None
    thinking: bool | None = None
