"""Streaming accumulator (§39.2, I26).

The single most bug-prone rule of the whole adapter layer lives here: **tool-call
arguments are accumulated as raw strings and parsed exactly once, at
``ToolCallEnd``**.  Partial JSON is by definition invalid, so ``json.loads`` must
never run on an intermediate fragment.

The accumulator is also where usage and the reasoning policy are resolved, and
it enforces I25/I11's prerequisite: two tool calls may not share a ``call_id``
inside one response (that would break result pairing), which is a provider bug
and therefore raises :class:`AdapterProtocolError`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Optional

from .canonical import (
    FinishDelta,
    LlmMessage,
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
)
from .errors import AdapterProtocolError


@dataclass
class _PendingCall:
    index: int
    call_id: str
    name: str
    args_parts: list = field(default_factory=list)
    arguments: Optional[dict] = None

    def parse_once(self) -> dict:
        if self.arguments is None:
            raw = "".join(self.args_parts)
            if not raw.strip():
                self.arguments = {}
            else:
                try:
                    self.arguments = json.loads(raw)
                except json.JSONDecodeError as exc:
                    raise AdapterProtocolError(
                        f"invalid tool-call arguments for {self.name!r} (index {self.index}): {exc}"
                    ) from exc
        return self.arguments


def _merge_usage(current: Usage, incoming: Optional[Usage]) -> Usage:
    """Field-wise **max** — robust to providers reporting usage once, split, or twice."""
    if incoming is None:
        return current
    return Usage(
        input_tokens=max(current.input_tokens, incoming.input_tokens),
        output_tokens=max(current.output_tokens, incoming.output_tokens),
        cache_read=max(current.cache_read, incoming.cache_read),
        cache_write=max(current.cache_write, incoming.cache_write),
        reasoning_tokens=max(current.reasoning_tokens, incoming.reasoning_tokens),
        estimated=current.estimated and incoming.estimated,
    )


class StreamAccumulator:
    """Collects canonical chunks into a final :class:`LlmResponse`."""

    def __init__(self, *, adapter, model: str, reasoning_policy: str = "strip") -> None:
        self.adapter = adapter
        self.model = model
        self.reasoning_policy = reasoning_policy
        self._text: list = []
        self._reasoning: list = []
        self._reasoning_sig: Optional[str] = None
        self._calls: dict = {}
        self._order: list = []
        self._ids: set = set()
        self.finish: Optional[str] = None
        self.usage = Usage(0, 0, estimated=True)
        self.model_actual: str = model
        self.response_id: Optional[str] = None

    # -- ingest -------------------------------------------------------------
    def feed(self, chunk) -> None:
        if isinstance(chunk, TextDelta):
            self._text.append(chunk.text)
        elif isinstance(chunk, ReasoningDelta):
            self._reasoning.append(chunk.text)
        elif isinstance(chunk, ReasoningSignature):
            self._reasoning_sig = chunk.signature
        elif isinstance(chunk, ToolCallStart):
            if chunk.call_id in self._ids:
                raise AdapterProtocolError(f"duplicate tool call_id in one response: {chunk.call_id}")
            self._ids.add(chunk.call_id)
            self._calls[chunk.index] = _PendingCall(chunk.index, chunk.call_id, chunk.name)
            self._order.append(chunk.index)
        elif isinstance(chunk, ToolCallArgsDelta):
            pending = self._calls.get(chunk.index)
            if pending is None:  # tolerate an args delta before a start
                pending = _PendingCall(chunk.index, f"{self.adapter.provider}-{chunk.index}", "")
                self._calls[chunk.index] = pending
                self._order.append(chunk.index)
            pending.args_parts.append(chunk.partial_json)  # accumulate, never parse
        elif isinstance(chunk, ToolCallEnd):
            if chunk.index == -1:
                for index in self._order:
                    self._calls[index].parse_once()  # parse exactly once
            elif chunk.index in self._calls:
                self._calls[chunk.index].parse_once()
        elif isinstance(chunk, MetaDelta):
            if chunk.model_actual:
                self.model_actual = chunk.model_actual
            if chunk.response_id:
                self.response_id = chunk.response_id
        elif isinstance(chunk, FinishDelta):
            self.finish = chunk.finish
            self.usage = _merge_usage(self.usage, chunk.usage)
        elif isinstance(chunk, UsageDelta):
            self.usage = _merge_usage(self.usage, chunk.usage)

    # -- resolve ------------------------------------------------------------
    def _resolved_reasoning(self) -> tuple:
        text = "".join(self._reasoning) or None
        if text is None or self.reasoning_policy == "strip":
            return None, None
        if self.reasoning_policy == "keep":
            return text, self._reasoning_sig
        if self.reasoning_policy == "summarize":
            return "(earlier reasoning omitted)", None
        return None, None

    def partial_message(self) -> LlmMessage:
        """Assistant message with only the text seen so far (never parses tool JSON).

        Used when a stream is cooperatively cancelled: partial tool-call arguments
        are by definition invalid JSON, so they are dropped rather than parsed
        (which would raise, I26) and only the visible text is preserved.
        """
        text = "".join(self._text)
        parts = (TextPart(text),) if text else ()
        return LlmMessage(role="assistant", parts=parts)

    def to_response(self) -> LlmResponse:
        text = "".join(self._text)
        parts = (TextPart(text),) if text else ()
        tool_calls = tuple(
            ToolCall(
                id=self._calls[i].call_id,
                name=self._calls[i].name,
                arguments=self._calls[i].parse_once(),
            )
            for i in self._order
        )
        reasoning, signature = self._resolved_reasoning()
        message = LlmMessage(
            role="assistant",
            parts=parts,
            tool_calls=tool_calls,
            reasoning=reasoning,
            reasoning_sig=signature,
        )
        finish = self.finish or ("tool_calls" if tool_calls else "stop")
        return LlmResponse(
            message=message,
            finish=finish,
            usage=self.usage,
            model_actual=self.model_actual,
            provider=self.adapter.provider,
            adapter_version=self.adapter.adapter_version,
        )

