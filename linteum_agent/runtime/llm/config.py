"""Provider configuration (§35, §38).

Mirrors ``linteum_agent/model/config.py``: one :class:`LLMConfig` describes a
provider endpoint, and the adapter layer turns it into an SDK client.

Two ideas make "all non-Anthropic vendors go through the OpenAI SDK" natural:

* every OpenAI-family provider is the *same* :class:`~linteum_agent.runtime.llm.adapters.openai.OpenAIAdapter`
  pointed at a different ``base_url`` (``openai`` / ``openai_compatible`` /
  ``deepseek`` / ``moonshot`` / ``qwen`` / ``zhipu`` / ``siliconflow`` …);
* ``openai_mode`` selects between the **Responses API** and **Chat Completions**,
  so a single adapter covers both dialects.

Provider defaults (endpoint, api-key env var, default mode) live in
:data:`PROVIDER_DEFAULTS` so an unknown vendor just needs ``base_url``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from typing import Literal, Optional

#: Which OpenAI dialect to speak.
OpenAIMode = Literal["responses", "chat_completions"]

#: Per-provider defaults. ``kind`` picks the adapter class family.
PROVIDER_DEFAULTS: dict[str, dict] = {
    "openai": {
        "kind": "openai", "base_url": None, "api_key_env": "OPENAI_API_KEY",
        "openai_mode": "responses",
    },
    "openai_compatible": {
        "kind": "openai", "base_url": None, "api_key_env": "OPENAI_API_KEY",
        "openai_mode": "chat_completions", "require_base_url": True,
    },
    "deepseek": {
        "kind": "deepseek", "base_url": "https://api.deepseek.com",
        "api_key_env": "DEEPSEEK_API_KEY", "openai_mode": "chat_completions",
    },
    # legacy alias — the DeepSeek provider accepts both (mirrors linteum_agent)
    "deepseek_compatible": {
        "kind": "deepseek", "base_url": "https://api.deepseek.com",
        "api_key_env": "DEEPSEEK_API_KEY", "openai_mode": "chat_completions",
    },
    # ── other vendors: same OpenAI SDK, different endpoint ──
    "moonshot": {
        "kind": "openai", "base_url": "https://api.moonshot.cn/v1",
        "api_key_env": "MOONSHOT_API_KEY", "openai_mode": "chat_completions",
    },
    "qwen": {
        "kind": "openai", "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "api_key_env": "DASHSCOPE_API_KEY", "openai_mode": "chat_completions",
    },
    "zhipu": {
        "kind": "openai", "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "api_key_env": "ZHIPU_API_KEY", "openai_mode": "chat_completions",
    },
    "siliconflow": {
        "kind": "openai", "base_url": "https://api.siliconflow.cn/v1",
        "api_key_env": "SILICONFLOW_API_KEY", "openai_mode": "chat_completions",
    },
    "anthropic": {
        "kind": "anthropic", "base_url": None, "api_key_env": "ANTHROPIC_API_KEY",
        "openai_mode": None,
    },
}


@dataclass
class LLMConfig:
    """Provider endpoint + default request parameters."""

    provider: str
    model: str
    openai_mode: Optional[OpenAIMode] = None      # None -> provider default
    api_key: Optional[str] = None                 # None -> env var
    base_url: Optional[str] = None                # None -> provider default
    timeout: float = 60.0

    temperature: Optional[float] = 0.7
    max_tokens: int = 4096
    top_p: Optional[float] = None
    reasoning_effort: Optional[str] = None
    thinking: Optional[bool] = None

    # -- resolution ---------------------------------------------------------
    @property
    def defaults(self) -> dict:
        return PROVIDER_DEFAULTS.get(self.provider, {})

    def adapter_kind(self) -> str:
        return self.defaults.get("kind", "openai")

    def resolved_api_key(self) -> Optional[str]:
        if self.api_key:
            return self.api_key
        env = self.defaults.get("api_key_env")
        return os.getenv(env) if env else None

    def resolved_base_url(self) -> Optional[str]:
        return self.base_url or self.defaults.get("base_url")

    def resolved_openai_mode(self) -> OpenAIMode:
        """Explicit mode wins; otherwise the provider default; otherwise chat."""
        if self.openai_mode:
            return self.openai_mode
        return self.defaults.get("openai_mode") or "chat_completions"

    def with_overrides(self, **kw) -> "LLMConfig":
        return replace(self, **kw)


def validate_config(config: LLMConfig) -> None:
    if not config.provider:
        raise ValueError("provider is required")
    if not config.model:
        raise ValueError("model is required")
    if config.defaults.get("require_base_url") and not config.resolved_base_url():
        raise ValueError(f"base_url is required when provider={config.provider!r}")
