"""Canonical, provider-neutral types (§36).

Everything above the adapter layer sees *only* these types (I22): the Agent
reducer, the ContextBuilder, the cache and the WAL never touch a provider's raw
field names.  All kinds of provider differences — message shape, tool-call
encoding, usage field names, stop reasons — are converted at the adapter
boundary, so ``on_llm`` in the agent can never contain ``if provider == ...``.

This module is pure data: frozen dataclasses, ``to_dict`` / ``from_dict`` (both
JSON-serialisable, P2) and one shared token estimator (I27).
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from enum import Enum, auto
from typing import Any, Literal, Optional, Union


def _opt_int(value: Any) -> Optional[int]:
    """``None`` stays ``None`` (unset); otherwise coerce to ``int``."""
    return None if value is None else int(value)


#: Normalised finish reasons — exactly five values (§36).
FinishReason = Literal["stop", "length", "tool_calls", "content_filter", "error"]

Role = Literal["system", "user", "assistant", "tool"]


class CanonicalError(Enum):
    """Provider errors collapsed to a small, actionable taxonomy (§41)."""

    AUTH = auto()
    RATE_LIMIT = auto()
    CONTEXT_OVERFLOW = auto()
    CONTENT_FILTER = auto()
    BAD_REQUEST = auto()
    PROVIDER_DOWN = auto()
    OVERLOADED = auto()
    TIMEOUT = auto()
    UNSUPPORTED = auto()
    PROTOCOL = auto()
    UNKNOWN = auto()


# ---------------------------------------------------------------------------
# Content parts
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TextPart:
    text: str


@dataclass(frozen=True)
class ImagePart:
    ref: str          # art: reference — never inline base64
    mime: str


@dataclass(frozen=True)
class DocPart:
    ref: str
    mime: str


ContentPart = Union[TextPart, ImagePart, DocPart]


def to_part_dict(part: ContentPart) -> dict:
    if isinstance(part, TextPart):
        return {"type": "text", "text": part.text}
    if isinstance(part, ImagePart):
        return {"type": "image", "ref": part.ref, "mime": part.mime}
    return {"type": "doc", "ref": part.ref, "mime": part.mime}


def part_from_dict(data: dict) -> ContentPart:
    kind = data.get("type")
    if kind == "image":
        return ImagePart(ref=data["ref"], mime=data["mime"])
    if kind == "doc":
        return DocPart(ref=data["ref"], mime=data["mime"])
    return TextPart(text=data.get("text", ""))


def parts_text(parts: tuple) -> str:
    """Concatenate the text of every :class:`TextPart` in ``parts``."""
    return "".join(p.text for p in parts if isinstance(p, TextPart))


# ---------------------------------------------------------------------------
# Tool call + message
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ToolCall:
    id: str                # normalised call_id (§37.4)
    name: str
    arguments: dict        # parsed — only ever populated at ToolCallEnd (I26)


@dataclass(frozen=True)
class LlmMessage:
    role: Role
    parts: tuple = ()                       # tuple[ContentPart, ...]
    tool_calls: tuple = ()                  # tuple[ToolCall, ...]
    tool_call_id: Optional[str] = None
    reasoning: Optional[str] = None         # kept or stripped per policy (§40)
    reasoning_sig: Optional[str] = None     # Anthropic thinking signature

    @property
    def text(self) -> str:
        return parts_text(self.parts)

    def to_dict(self) -> dict:
        data: dict = {"role": self.role, "content": [to_part_dict(p) for p in self.parts]}
        if self.tool_calls:
            data["tool_calls"] = [
                {"id": tc.id, "name": tc.name, "arguments": tc.arguments} for tc in self.tool_calls
            ]
        if self.tool_call_id is not None:
            data["tool_call_id"] = self.tool_call_id
        if self.reasoning is not None:
            data["reasoning"] = self.reasoning
        if self.reasoning_sig is not None:
            data["reasoning_sig"] = self.reasoning_sig
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "LlmMessage":
        role = data.get("role", "user")
        content = data.get("content", "")
        if isinstance(content, str):
            parts = (TextPart(content),) if content else ()
        else:
            parts = tuple(part_from_dict(p) for p in content)
        tool_calls = tuple(
            ToolCall(id=c["id"], name=c["name"], arguments=dict(c.get("arguments") or {}))
            for c in data.get("tool_calls", [])
        )
        return cls(
            role=role,
            parts=parts,
            tool_calls=tool_calls,
            tool_call_id=data.get("tool_call_id"),
            reasoning=data.get("reasoning"),
            reasoning_sig=data.get("reasoning_sig"),
        )

    @classmethod
    def text_message(cls, role: Role, text: str, **kw: Any) -> "LlmMessage":
        return cls(role=role, parts=(TextPart(text),), **kw)


def message_to_json(message: LlmMessage) -> dict:
    return message.to_dict()


# ---------------------------------------------------------------------------
# Request / response / usage
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class LlmRequest:
    model: str                                   # alias, e.g. "default"
    messages: tuple = ()                         # tuple[LlmMessage, ...]
    tools: tuple = ()                            # tuple[dict, ...] JSON Schema
    tool_choice: Any = None
    max_tokens: Optional[int] = None             # None = unset -> adapter config default
    temperature: Optional[float] = None
    top_p: Optional[float] = None
    stop: tuple = ()
    response_format: Optional[dict] = None
    stream: bool = True
    cache_plan: tuple = ()                       # block kinds needing a cache breakpoint
    # host-set trace seed (the ``llm.request`` event id); used for deterministic
    # synthetic call ids (§37.4) and intentionally EXCLUDED from ``to_dict`` so it
    # never perturbs the idempotency key (I28).
    request_id: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "model": self.model,
            "messages": [m.to_dict() for m in self.messages],
            "tools": list(self.tools),
            "tool_choice": self.tool_choice,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "top_p": self.top_p,
            "stop": list(self.stop),
            "response_format": self.response_format,
            "stream": self.stream,
            "cache_plan": list(self.cache_plan),
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True)

    @classmethod
    def from_dict(cls, data: dict) -> "LlmRequest":
        return cls(
            model=data["model"],
            messages=tuple(LlmMessage.from_dict(m) for m in data.get("messages", [])),
            tools=tuple(data.get("tools", ())),
            tool_choice=data.get("tool_choice"),
            max_tokens=_opt_int(data.get("max_tokens")),
            temperature=data.get("temperature"),
            top_p=data.get("top_p"),
            stop=tuple(data.get("stop", ())),
            response_format=data.get("response_format"),
            stream=bool(data.get("stream", True)),
            cache_plan=tuple(data.get("cache_plan", ())),
        )

    @classmethod
    def from_event(cls, ev) -> "LlmRequest":
        """Build the canonical request from an ``llm.request`` event payload."""
        p = ev.payload
        return cls(
            model=p.get("model", "default"),
            messages=tuple(LlmMessage.from_dict(m) for m in p.get("messages", [])),
            tools=tuple(p.get("tools", ())),
            tool_choice=p.get("tool_choice"),
            max_tokens=_opt_int(p.get("max_tokens")),
            temperature=p.get("temperature"),
            top_p=p.get("top_p"),
            stop=tuple(p.get("stop", ())),
            response_format=p.get("response_format"),
            stream=bool(p.get("stream", True)),
            cache_plan=tuple(p.get("cache_plan", ())),
            request_id=ev.id,
        )

    def replace(self, **kw: Any) -> "LlmRequest":
        from dataclasses import replace as _replace

        return _replace(self, **kw)


@dataclass(frozen=True)
class Usage:
    input_tokens: int
    output_tokens: int
    cache_read: int = 0          # Anthropic cache_read / OpenAI cached_tokens / DS cache_hit
    cache_write: int = 0
    reasoning_tokens: int = 0
    estimated: bool = False      # set on stream interruption (§39.5) — never silently

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens

    def to_dict(self) -> dict:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read": self.cache_read,
            "cache_write": self.cache_write,
            "reasoning_tokens": self.reasoning_tokens,
            "estimated": self.estimated,
            "total_tokens": self.total,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Usage":
        return cls(
            input_tokens=int(data.get("input_tokens", 0)),
            output_tokens=int(data.get("output_tokens", 0)),
            cache_read=int(data.get("cache_read", 0)),
            cache_write=int(data.get("cache_write", 0)),
            reasoning_tokens=int(data.get("reasoning_tokens", 0)),
            estimated=bool(data.get("estimated", False)),
        )


@dataclass(frozen=True)
class LlmResponse:
    message: LlmMessage
    finish: str
    usage: Usage
    model_actual: str
    provider: str
    adapter_version: str
    raw_ref: Optional[str] = None    # art: reference for debugging (non-semantic)


@dataclass(frozen=True)
class Cost:
    input_usd: float
    output_usd: float
    cache_usd: float
    total_usd: float
    pricing_version: str

    def to_dict(self) -> dict:
        return {
            "input_usd": self.input_usd,
            "output_usd": self.output_usd,
            "cache_usd": self.cache_usd,
            "total_usd": self.total_usd,
            "pricing_version": self.pricing_version,
        }


# ---------------------------------------------------------------------------
# Capabilities / health (§37.1)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Capabilities:
    """Declared capabilities — **from config, not hard-coded** (§37.1)."""

    ctx_window: int = 0
    max_output: int = 4096                      # 0 = unknown -> LLMExecutor._clamp skips
    tools: bool = True
    strict_schema: bool = False
    json_mode: bool = False
    reasoning: bool = False
    cache_mode: str = "none"                    # none | auto | explicit
    max_cache_breakpoints: int = 0
    params_supported: frozenset = frozenset()   # e.g. {"temperature","top_p"}
    stream_usage: bool = False
    stream_partial_json_args: bool = True
    reasoning_history_policy: str = "strip"     # strip | keep | summarize
    pricing_version: str = ""


@dataclass(frozen=True)
class Health:
    ok: bool
    detail: str = ""


# ---------------------------------------------------------------------------
# Canonical streaming chunks (§39.2)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TextDelta:
    text: str


@dataclass(frozen=True)
class ReasoningDelta:
    text: str


@dataclass(frozen=True)
class ReasoningSignature:
    signature: str


@dataclass(frozen=True)
class ToolCallStart:
    index: int
    call_id: str
    name: str


@dataclass(frozen=True)
class ToolCallArgsDelta:
    index: int
    partial_json: str


@dataclass(frozen=True)
class ToolCallEnd:
    index: int


@dataclass(frozen=True)
class MetaDelta:
    """Provider metadata (real model id / response id); never produces UI events."""

    model_actual: Optional[str] = None
    response_id: Optional[str] = None


@dataclass(frozen=True)
class FinishDelta:
    finish: str
    usage: Optional[Usage] = None


@dataclass(frozen=True)
class UsageDelta:
    usage: Usage


LlmChunk = Union[
    TextDelta,
    ReasoningDelta,
    ReasoningSignature,
    ToolCallStart,
    ToolCallArgsDelta,
    ToolCallEnd,
    MetaDelta,
    FinishDelta,
    UsageDelta,
]


# ---------------------------------------------------------------------------
# Token estimation (single source of truth — I27)
# ---------------------------------------------------------------------------

def estimate_tokens(text: str) -> int:
    """Deterministic heuristic used by *both* the adapter and the ContextBuilder.

    I27 forbids the budget estimator and ``count_tokens`` from diverging, so both
    must call this one function.  A real tokenizer is a drop-in replacement here
    (and only here) once one is available.
    """
    if not text:
        return 0
    return max(1, math.ceil(len(text) / 4))


