"""LLM adapter layer (design doc §35–§45), implemented on the official SDKs.

Public surface:

* :mod:`linteum_agent.runtime.llm.canonical` — provider-neutral types (I22).
* :mod:`linteum_agent.runtime.llm.config` — :class:`LLMConfig` + provider defaults (§35/§38).
* :mod:`linteum_agent.runtime.llm.adapters` — OpenAI (Responses **and** Chat Completions),
  OpenAI-compatible, DeepSeek (OpenAI SDK) and Anthropic (Anthropic SDK).
* :mod:`linteum_agent.runtime.llm.accumulator` — chunk -> response, parsing tool args once (I26).
* :mod:`linteum_agent.runtime.llm.registry` / :mod:`linteum_agent.runtime.llm.router` — adapters, aliases, pinning (I25).
* :mod:`linteum_agent.runtime.llm.testing` — offline SDK fakes for fixture-style tests (§45.1).
* :mod:`linteum_agent.runtime.llm.executor` — :class:`LLMExecutor`, the only ``llm.request`` subscriber.

All non-Anthropic vendors go through the ``openai`` SDK; Anthropic uses the
``anthropic`` SDK.
"""

from .accumulator import StreamAccumulator
from .adapters import (
    AnthropicAdapter,
    BaseAdapter,
    DeepSeekAdapter,
    OpenAIAdapter,
    OpenAICompatibleAdapter,
    create_adapter,
    to_anthropic_message,
    to_openai_message,
    to_responses_input,
)
from .breaker import ProviderCircuitBreaker
from .cache import LlmCache, request_hash
from .canonical import (
    CanonicalError,
    Capabilities,
    Cost,
    DocPart,
    FinishDelta,
    FinishReason,
    Health,
    ImagePart,
    LlmChunk,
    LlmMessage,
    LlmRequest,
    LlmResponse,
    MetaDelta,
    ReasoningDelta,
    ReasoningSignature,
    TextDelta,
    TextPart,
    ToolCall,
    ToolCallArgsDelta,
    ToolCallEnd,
    ToolCallStart,
    Usage,
    UsageDelta,
    estimate_tokens,
)
from .config import PROVIDER_DEFAULTS, LLMConfig, OpenAIMode, validate_config
from .errors import (
    AdapterProtocolError,
    DuplicateAdapter,
    LLMError,
    NoAdapter,
    ProviderError,
    RetryPolicy,
    UnsupportedCapability,
    code_for,
    policy_for,
)
from .executor import LLMExecutor, exception_detail, redact_request
from .pricing import DEFAULT_PRICING, PRICING_VERSION, cost_for
from .registry import AdapterRegistry, Route, satisfies
from .router import LlmRouter
from . import testing

__all__ = [
    # canonical types
    "CanonicalError",
    "Capabilities",
    "Cost",
    "DocPart",
    "ImagePart",
    "TextPart",
    "LlmChunk",
    "LlmMessage",
    "LlmRequest",
    "LlmResponse",
    "Usage",
    "Health",
    "FinishReason",
    "TextDelta",
    "ReasoningDelta",
    "ReasoningSignature",
    "ToolCall",
    "ToolCallStart",
    "ToolCallArgsDelta",
    "ToolCallEnd",
    "MetaDelta",
    "FinishDelta",
    "UsageDelta",
    "estimate_tokens",
    # config
    "LLMConfig",
    "OpenAIMode",
    "PROVIDER_DEFAULTS",
    "validate_config",
    # adapters (SDK-based)
    "BaseAdapter",
    "OpenAIAdapter",
    "OpenAICompatibleAdapter",
    "DeepSeekAdapter",
    "AnthropicAdapter",
    "create_adapter",
    "to_openai_message",
    "to_responses_input",
    "to_anthropic_message",
    "StreamAccumulator",
    # registry / routing / resilience
    "AdapterRegistry",
    "Route",
    "satisfies",
    "LlmRouter",
    "ProviderCircuitBreaker",
    "LlmCache",
    "request_hash",
    "cost_for",
    "PRICING_VERSION",
    "DEFAULT_PRICING",
    # executor
    "LLMExecutor",
    "redact_request",
    "exception_detail",
    # errors
    "LLMError",
    "ProviderError",
    "AdapterProtocolError",
    "UnsupportedCapability",
    "DuplicateAdapter",
    "NoAdapter",
    "RetryPolicy",
    "policy_for",
    "code_for",
    # offline testing helpers
    "testing",
]
