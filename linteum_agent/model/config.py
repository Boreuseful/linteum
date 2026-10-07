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

# Reasoning effort for DeepSeek chain-of-thought (thinking mode).
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

    # Chain-of-thought (reasoning / thinking mode) configuration.
    # Under Chat Completions, DeepSeek maps this to reasoning_effort + extra_body.thinking,
    # and under the Responses API it maps to reasoning.effort.
    reasoning_effort: ReasoningEffort | str | None = None
    thinking: bool | None = None
