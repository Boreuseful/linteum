"""Context plan: a *derived view*, never state (§29–§31, P11).

A full ContextBuilder (budget allocation, deterministic compaction, retrieval
materialisation) is stage S7–S9.  This module defines the **interface and the
replay anchor** the agent loop already depends on:

* :class:`ContextPlan` — the block sequence handed to the provider, carrying a
  ``digest``;
* **I13** — the digest is computed deterministically from the inputs, so a replay
  recomputes the identical value and any divergence is detectable;
* **I27** — token estimation routes through the *one* shared estimator.

The default builder is a pass-through over the state's messages plus the tool
schemas; later builders extend the digest to cover the system prompt and pinned
blocks without changing the agent.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Protocol, Sequence

from .llm.canonical import estimate_tokens


def _canonical_json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


@dataclass(frozen=True)
class ContextPlan:
    """A provider-ready message sequence plus its replay digest."""

    messages: tuple
    digest: str
    tools: tuple = ()

    def to_provider_messages(self) -> list:
        return [dict(m) for m in self.messages]

    def to_dict(self) -> dict:
        return {"messages": self.to_provider_messages(), "digest": self.digest, "tools": list(self.tools)}

    def estimated_tokens(self) -> int:
        total = 0
        for message in self.messages:
            if isinstance(message.get("content"), str):
                total += estimate_tokens(message["content"])
            else:
                total += estimate_tokens(_canonical_json(message.get("content", "")))
        for tool in self.tools:
            total += estimate_tokens(_canonical_json(tool))
        return total


class ContextBuilder(Protocol):
    async def build(self, state, *, ev=None, tools: Sequence = ()) -> ContextPlan: ...


class PassthroughContextBuilder:
    """Minimal builder: state messages in, provider messages + digest out."""

    async def build(self, state, *, ev=None, tools: Sequence = ()) -> ContextPlan:
        messages = tuple(self._normalise(m) for m in state.messages)
        tools_tuple = tuple(tools)
        digest = self.digest(messages, tools_tuple)
        return ContextPlan(messages, digest, tools_tuple)

    @staticmethod
    def _normalise(message: dict) -> dict:
        normalised = {"role": message.get("role", "user")}
        content = message.get("content", "")
        normalised["content"] = content
        if message.get("tool_calls"):
            normalised["tool_calls"] = message["tool_calls"]
        if message.get("tool_call_id") is not None:
            normalised["tool_call_id"] = message["tool_call_id"]
        if message.get("reasoning") is not None:
            normalised["reasoning"] = message["reasoning"]
        return normalised

    @staticmethod
    def digest(messages: tuple, tools: tuple = ()) -> str:
        """Deterministic anchor (I13): same inputs -> same digest, always."""
        blob = _canonical_json({"messages": list(messages), "tools": list(tools)})
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()
