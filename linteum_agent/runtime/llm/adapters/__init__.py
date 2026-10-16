"""Adapters (§38). All OpenAI-family providers use the ``openai`` SDK; Anthropic
uses the ``anthropic`` SDK.  Use :func:`create_adapter` to build one from an
:class:`~linteum_agent.runtime.llm.config.LLMConfig`."""

from .anthropic import AnthropicAdapter
from .base import (
    BaseAdapter,
    field,
    to_anthropic_message,
    to_openai_message,
    to_responses_input,
)
from .deepseek import DeepSeekAdapter
from .openai import OpenAIAdapter, OpenAICompatibleAdapter

__all__ = [
    "BaseAdapter",
    "OpenAIAdapter",
    "OpenAICompatibleAdapter",
    "DeepSeekAdapter",
    "AnthropicAdapter",
    "create_adapter",
    "to_openai_message",
    "to_responses_input",
    "to_anthropic_message",
    "field",
]


def create_adapter(config, *, client=None, **kwargs):
    """Build the right adapter for ``config`` (§38).

    * ``anthropic``               -> :class:`AnthropicAdapter`
    * ``deepseek``                -> :class:`DeepSeekAdapter`
    * ``openai_compatible``       -> :class:`OpenAICompatibleAdapter` (needs ``base_url``)
    * ``openai`` / any other vendor -> :class:`OpenAIAdapter` (endpoint from config)
    """
    kind = config.adapter_kind()
    if kind == "anthropic":
        return AnthropicAdapter(config, client=client, **kwargs)
    if kind == "deepseek":
        return DeepSeekAdapter(config, client=client, **kwargs)
    if config.provider == "openai_compatible":
        return OpenAICompatibleAdapter(config, client=client, **kwargs)
    return OpenAIAdapter(config, client=client, **kwargs)
