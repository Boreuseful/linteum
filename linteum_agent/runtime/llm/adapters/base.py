"""Adapter base: SDK-backed providers (mirrors ``linteum_agent/model/llm.py``).

Provider differences (auth, endpoints, message shape, tool encoding, stream
frames, usage fields, stop reasons) are absorbed here so the Agent reducer never
sees them (I22).  The implementation talks to the official SDKs
(``openai.AsyncOpenAI`` / ``anthropic.AsyncAnthropic``) rather than raw HTTP:
SSE framing, incremental UTF-8 and connection pooling are the SDK's job, and
only *field mapping* remains ours.

A provider is a pure translator plus an SDK client: it never publishes to the
bus and never reads ``AgentState`` (I21).

Two seams keep everything testable offline:

* ``build_body(req) -> (kwargs, dropped)`` — the exact SDK kwargs, JSON
  serialisable, used both to call the SDK and to record the redacted
  ``llm.started.request_preview`` (§37.3 / §38.4);
* ``client`` may be injected (see :mod:`linteum_agent.runtime.llm.testing`), which is how the
  contract tests replay recorded responses with no network (§45.1).
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, AsyncIterator, Optional

from ..canonical import (
    Capabilities,
    CanonicalError,
    Cost,
    DocPart,
    Health,
    ImagePart,
    LlmChunk,
    LlmMessage,
    LlmRequest,
    LlmResponse,
    TextPart,
    Usage,
    estimate_tokens,
)
from ..pricing import DEFAULT_PRICING, cost_for

#: Keyword hints that turn a generic 400 into CONTEXT_OVERFLOW (§41.1).
OVERFLOW_HINTS = (
    "context length",
    "maximum context",
    "too many tokens",
    "context_length_exceeded",
    "prompt is too long",
    "exceed",
)


def field(value: Any, name: str, default: Any = None) -> Any:
    """Read ``name`` from an object or a dict (SDK objects vs plain dicts)."""
    if value is None:
        return default
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def function_of(tool: dict) -> dict:
    """Return the inner function schema of a tool declaration (accept both shapes).

    Two shapes reach the adapters, and both must work:

    * **OpenAI-wrapped** — ``{"type": "function", "function": {name, ...}}``
      (what :meth:`linteum_agent.runtime.tools.spec.ToolSpec.json_schema` / ``ToolRegistry.schemas``
      produce, per §21);
    * **bare function object** — ``{"name", "description", "parameters"}``
      (hand-written schemas, e.g. the older examples).

    Without this normalisation a wrapped tool gets wrapped *again* and the
    provider rejects ``tools[0].function`` for a missing ``name`` — exactly the
    DeepSeek 422 ("missing field `name`") / OpenAI 400 seen in the wild.
    """
    if isinstance(tool, dict):
        inner = tool.get("function")
        if isinstance(inner, dict):
            return inner
    return tool


# ---------------------------------------------------------------------------
# Canonical message -> provider payloads
# ---------------------------------------------------------------------------

def to_openai_message(message: LlmMessage) -> dict:
    """canonical message -> OpenAI Chat Completions message."""
    data: dict = {"role": message.role}
    has_media = any(not isinstance(p, TextPart) for p in message.parts)
    if not has_media:
        data["content"] = message.text
    else:
        content: list = []
        for part in message.parts:
            if isinstance(part, TextPart):
                content.append({"type": "text", "text": part.text})
            else:
                content.append({"type": "image_url", "image_url": {"url": part.ref}})
        data["content"] = content
    if message.tool_calls:
        data["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {
                    "name": call.name,
                    "arguments": json.dumps(call.arguments, ensure_ascii=False),
                },
            }
            for call in message.tool_calls
        ]
    if message.tool_call_id is not None:
        data["tool_call_id"] = message.tool_call_id
    return data


def to_responses_input(message: LlmMessage) -> list:
    """canonical message -> OpenAI Responses API ``input`` items."""
    if message.role == "tool":
        return [{
            "type": "function_call_output",
            "call_id": message.tool_call_id,
            "output": message.text,
        }]
    if message.role == "assistant":
        items: list = []
        if message.reasoning:
            items.append({
                "type": "reasoning",
                "content": [{"type": "reasoning_text", "text": message.reasoning}],
            })
        if message.text:
            items.append({"role": "assistant", "content": message.text})
        items.extend(
            {
                "type": "function_call",
                "call_id": call.id,
                "name": call.name,
                "arguments": json.dumps(call.arguments, ensure_ascii=False),
            }
            for call in message.tool_calls
        )
        return items or [{"role": "assistant", "content": ""}]
    has_media = any(not isinstance(p, TextPart) for p in message.parts)
    if not has_media:
        return [{"role": message.role, "content": message.text}]
    content: list = []
    for part in message.parts:
        if isinstance(part, TextPart):
            content.append({"type": "input_text", "text": part.text})
        else:
            content.append({"type": "input_image", "image_url": part.ref})
    return [{"role": message.role, "content": content}]


def to_anthropic_message(message: LlmMessage) -> Optional[dict]:
    """canonical message -> Anthropic Messages payload (``system`` handled separately)."""
    if message.role == "system":
        return None
    if message.role == "tool":
        return {
            "role": "user",
            "content": [{
                "type": "tool_result",
                "tool_use_id": message.tool_call_id,
                "content": message.text,
            }],
        }
    blocks: list = []
    if message.role == "assistant" and message.reasoning:
        block: dict = {"type": "thinking", "thinking": message.reasoning}
        if message.reasoning_sig:
            block["signature"] = message.reasoning_sig
        blocks.append(block)
    for part in message.parts:
        if isinstance(part, TextPart):
            blocks.append({"type": "text", "text": part.text})
        else:
            blocks.append({"type": "image", "source": {"type": "url", "url": part.ref}})
    for call in message.tool_calls:
        blocks.append({"type": "tool_use", "id": call.id, "name": call.name, "input": call.arguments})
    return {"role": "assistant" if message.role == "assistant" else "user", "content": blocks}


# ---------------------------------------------------------------------------
# BaseAdapter
# ---------------------------------------------------------------------------

class BaseAdapter:
    """Provider translator + SDK client; subclasses implement ``build_body``/``astream``."""

    provider: str = "base"
    adapter_version: str = "1.0.0"

    def __init__(
        self,
        config=None,
        *,
        capabilities_map: Optional[dict] = None,
        default_capabilities: Optional[Capabilities] = None,
        pricing: Optional[dict] = None,
        client=None,
    ) -> None:
        self.config = config
        self.client = client
        self._caps = capabilities_map or {}
        self._default_caps = default_capabilities or Capabilities()
        self.pricing = pricing or DEFAULT_PRICING
        provider = getattr(config, "provider", None)
        if provider:
            self.provider = provider

    # -- capabilities / metering -------------------------------------------
    def _model_id(self, model: str) -> str:
        """Map a canonical alias to a concrete provider model id (identity by default)."""
        return model

    def capabilities(self, model: str) -> Capabilities:
        override = self._caps.get(model) or self._caps.get("*")
        return replace_caps(self._default_caps, override) if override else self._default_caps

    def count_tokens(self, messages, model: str) -> int:
        """Estimate prompt tokens — must share the estimator with the ContextBuilder (I27)."""
        total = 0
        for message in messages:
            total += estimate_tokens(message.text)
            if message.reasoning:
                total += estimate_tokens(message.reasoning)
            for call in message.tool_calls:
                total += estimate_tokens(
                    json.dumps(call.arguments, ensure_ascii=False, sort_keys=True)
                )
        return max(1, total)

    def estimate_cost(self, usage: Usage, model: str) -> Cost:
        return cost_for(usage, model, self.pricing)

    # -- diagnostics --------------------------------------------------------
    def classify_status(self, status: int, body: bytes = b"") -> CanonicalError:
        """``(status, keyword)`` classification (§41.1)."""
        text = body.decode("utf-8", "replace").lower()
        if status in (401, 403):
            return CanonicalError.AUTH
        if status == 429:
            return CanonicalError.RATE_LIMIT
        if status in (400, 422):
            if any(hint in text for hint in OVERFLOW_HINTS):
                return CanonicalError.CONTEXT_OVERFLOW
            return CanonicalError.BAD_REQUEST
        if status in (503, 529):
            return CanonicalError.OVERLOADED
        if status == 408:
            return CanonicalError.TIMEOUT
        if status >= 500:
            return CanonicalError.PROVIDER_DOWN
        return CanonicalError.UNKNOWN

    def classify_error(self, error: BaseException) -> CanonicalError:
        """Classify an SDK exception by duck-typing (no SDK import required here)."""
        if isinstance(error, CanonicalError):
            return error
        name = type(error).__name__
        status = getattr(error, "status_code", None) or getattr(
            getattr(error, "response", None), "status_code", None
        )
        try:
            text = str(error)
        except Exception:  # noqa: BLE001 - best effort
            text = ""

        if isinstance(error, TimeoutError) or "Timeout" in name:
            return CanonicalError.TIMEOUT
        if "Connection" in name:
            return CanonicalError.PROVIDER_DOWN
        if status:
            return self.classify_status(int(status), text.encode("utf-8", "replace"))
        if name in ("AuthenticationError", "PermissionDeniedError"):
            return CanonicalError.AUTH
        if name == "RateLimitError":
            return CanonicalError.RATE_LIMIT
        if name in ("InternalServerError", "OverloadedError"):
            return CanonicalError.OVERLOADED
        if name in ("BadRequestError", "UnprocessableEntityError", "ValidationError"):
            lowered = text.lower()
            if any(hint in lowered for hint in OVERFLOW_HINTS):
                return CanonicalError.CONTEXT_OVERFLOW
            return CanonicalError.BAD_REQUEST
        return CanonicalError.UNKNOWN

    async def health(self) -> Health:
        return Health(ok=True, detail=f"{self.provider} adapter ready")

    # -- SDK call seam ------------------------------------------------------
    def build_body(self, req: LlmRequest) -> tuple:
        """Return ``(sdk_kwargs, dropped_param_names)`` — kwargs must be JSON-serialisable."""
        raise NotImplementedError

    async def astream(self, req: LlmRequest) -> AsyncIterator[LlmChunk]:
        """Stream canonical chunks for ``req`` from the provider SDK."""
        raise NotImplementedError
        yield  # pragma: no cover - makes this an async generator for subclasses

    async def acomplete(self, req: LlmRequest) -> LlmResponse:
        """Non-streaming is "a stream of one" (§39.4) — same chunk interface."""
        from ..accumulator import StreamAccumulator

        acc = StreamAccumulator(
            adapter=self,
            model=req.model,
            reasoning_policy=self.capabilities(req.model).reasoning_history_policy,
        )
        async for chunk in self.astream(req):
            acc.feed(chunk)
        return acc.to_response()

    # -- helpers ------------------------------------------------------------
    def _apply_sampling(self, body: dict, req: LlmRequest) -> list:
        """Copy only the sampling params the model declares it supports (§38.4)."""
        supported = self.capabilities(req.model).params_supported
        dropped: list = []
        for key in ("temperature", "top_p", "stop"):
            value = getattr(req, key, None)
            if value in (None, (), {}):
                continue
            if key in supported:
                body[key] = list(value) if key == "stop" else value
            else:
                dropped.append(key)
        return dropped

    def _max_tokens(self, req: LlmRequest) -> Optional[int]:
        """Effective ``max_tokens``: the request value, else the config default.

        ``LlmConfig.max_tokens`` / ``LLMConfig.max_tokens`` is the provider-level
        default; an explicit ``req.max_tokens`` (from the ``llm.request`` event)
        overrides it.  May be ``None`` when neither is set.
        """
        if req.max_tokens is not None:
            return req.max_tokens
        return getattr(self.config, "max_tokens", None)

    def _synth_id(self, model: str, index: int, seed: Optional[str]) -> str:
        """Deterministic synthetic call id from the request seed (§37.4)."""
        raw = f"{self.provider}:{seed or 'noseed'}:{index}"
        return f"{self.provider}-{hashlib.sha256(raw.encode()).hexdigest()[:16]}"


def replace_caps(base: Capabilities, override: dict) -> Capabilities:
    from dataclasses import replace

    return replace(base, **override)



