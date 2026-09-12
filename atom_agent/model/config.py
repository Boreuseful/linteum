from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


ProviderName = Literal[
    "openai",
    "openai_compatible",
    "anthropic",
]

OpenAIMode = Literal[
    "responses",
    "chat_completions",
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