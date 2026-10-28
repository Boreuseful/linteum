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
from typing import Optional, Protocol, Sequence

from .llm.canonical import estimate_tokens


def _canonical_json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


@dataclass(frozen=True, slots=True)
class ContextPolicy:
    """Token budget + compaction policy for one agent session.

    ``max_tokens`` is the budget the host allocates for the provider context
    (prompt + tool schemas), not the model's raw context window.  ``0`` disables
    compaction, preserving the old pass-through behaviour.
    """

    max_tokens: int = 0
    compact_to_tokens: int = 0
    keep_last_messages: int = 8
    max_summary_tokens: int = 768

    def __post_init__(self) -> None:
        if self.max_tokens < 0:
            raise ValueError("max_tokens must be >= 0")
        if self.compact_to_tokens < 0:
            raise ValueError("compact_to_tokens must be >= 0")
        if self.max_tokens and self.compact_to_tokens >= self.max_tokens:
            raise ValueError("compact_to_tokens must be < max_tokens")
        if self.keep_last_messages < 1:
            raise ValueError("keep_last_messages must be >= 1")
        if self.max_summary_tokens < 1:
            raise ValueError("max_summary_tokens must be >= 1")

    @property
    def enabled(self) -> bool:
        return self.max_tokens > 0

    @property
    def target_tokens(self) -> int:
        if not self.max_tokens:
            return 0
        return self.compact_to_tokens or max(1, int(self.max_tokens * 0.7))


@dataclass(frozen=True, slots=True)
class CompactionRange:
    """Inclusive-exclusive slice of ``state.messages`` to replace by a summary."""

    from_index: int
    to_index: int

    def __bool__(self) -> bool:
        return self.to_index > self.from_index


def _message_tokens(message: dict) -> int:
    content = message.get("content", "")
    if isinstance(content, str):
        total = estimate_tokens(content)
    else:
        total = estimate_tokens(_canonical_json(content))
    for call in message.get("tool_calls") or []:
        total += estimate_tokens(_canonical_json(call))
    return total


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


def normalise_message(message: dict) -> dict:
    """Project an internal state message onto the provider-safe message shape."""
    normalised = {"role": message.get("role", "user")}
    normalised["content"] = message.get("content", "")
    if message.get("tool_calls"):
        normalised["tool_calls"] = message["tool_calls"]
    if message.get("tool_call_id") is not None:
        normalised["tool_call_id"] = message["tool_call_id"]
    if message.get("reasoning") is not None:
        normalised["reasoning"] = message["reasoning"]
    return normalised


class ContextBuilder(Protocol):
    async def build(self, state, *, ev=None, tools: Sequence = ()) -> ContextPlan: ...


class PassthroughContextBuilder:
    """Minimal builder: state messages in, provider messages + digest out.

    ``system_prompt`` is the mode body (when one is bound); ``system_blocks``
    are host-owned, already-rendered system messages (the environment block
    and the skills L0 index).  They are prepended in that order on every build
    and are therefore part of the deterministic ``context.digest``; they never
    enter ``AgentState.messages``.
    """

    def __init__(
        self,
        *,
        system_prompt: Optional[str] = None,
        system_blocks: Sequence[str] = (),
    ) -> None:
        self.system_prompt = system_prompt
        self.system_blocks = (
            (system_blocks,) if isinstance(system_blocks, str)
            else tuple(system_blocks)
        )

    async def build(self, state, *, ev=None, tools: Sequence = ()) -> ContextPlan:
        messages: list = []
        if self.system_prompt:
            messages.append({"role": "system", "content": self.system_prompt})
        messages.extend(
            {"role": "system", "content": block}
            for block in self.system_blocks
            if block
        )
        messages.extend(self._normalise(m) for m in state.messages)
        tools_tuple = tuple(tools)
        digest = self.digest(tuple(messages), tools_tuple)
        return ContextPlan(tuple(messages), digest, tools_tuple)

    @staticmethod
    def _normalise(message: dict) -> dict:
        return normalise_message(message)

    @staticmethod
    def digest(messages: tuple, tools: tuple = ()) -> str:
        """Deterministic anchor (I13): same inputs -> same digest, always."""
        blob = _canonical_json({"messages": list(messages), "tools": list(tools)})
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class CompactingContextBuilder(PassthroughContextBuilder):
    """Pass-through builder that replaces compacted message ranges by summaries.

    Compaction records are stored on the state (full source messages remain in
    the append-only history); this builder only decides what the *provider* sees.
    ``next_compaction`` picks a whole-turn prefix deterministically, so the agent
    can request a summary without any I/O or provider access in the reducer.
    """

    def __init__(
        self,
        *,
        system_prompt: Optional[str] = None,
        system_blocks: Sequence[str] = (),
    ) -> None:
        self.system_prompt = system_prompt
        self.system_blocks = (
            (system_blocks,) if isinstance(system_blocks, str)
            else tuple(system_blocks)
        )

    async def build(self, state, *, ev=None, tools: Sequence = ()) -> ContextPlan:
        messages: list = []
        if self.system_prompt:
            messages.append({"role": "system", "content": self.system_prompt})
        messages.extend(
            {"role": "system", "content": block}
            for block in self.system_blocks
            if block
        )
        records = {
            int(record["from_index"]): record
            for record in state.compactions
            if "from_index" in record and "to_index" in record
        }
        source = state.messages
        index = 0
        while index < len(source):
            record = records.get(index)
            if record is not None:
                end = int(record["to_index"])
                if end > index:
                    messages.append(self._summary_message(record))
                    index = end
                    continue
            messages.append(self._normalise(source[index]))
            index += 1
        tools_tuple = tuple(tools)
        digest = self.digest(tuple(messages), tools_tuple)
        return ContextPlan(tuple(messages), digest, tools_tuple)

    def next_compaction(
        self, state, plan: ContextPlan, policy: ContextPolicy
    ) -> Optional[CompactionRange]:
        """Return the next whole-turn prefix to compact, or ``None``."""
        current = plan.estimated_tokens()
        if not policy.enabled or current <= policy.max_tokens:
            return None
        messages = state.messages
        last_to = max(
            (int(record["to_index"]) for record in state.compactions), default=0
        )
        if last_to >= len(messages):
            return None
        keep = max(1, policy.keep_last_messages)
        max_end = min(len(messages) - keep, self._last_user_index(messages))
        if max_end <= last_to:
            return None
        boundaries = [
            index
            for index in range(last_to + 1, max_end + 1)
            if messages[index].get("role") == "user"
        ]
        if not boundaries:
            return None
        target = policy.target_tokens
        summary_tokens = max(1, policy.max_summary_tokens)
        chosen = None
        for end in boundaries:
            compacted = sum(
                _message_tokens(message) for message in messages[last_to:end]
            )
            if compacted <= summary_tokens:
                continue
            chosen = end
            if current - compacted + summary_tokens <= target:
                break
        if chosen is None:
            return None
        return CompactionRange(last_to, chosen)

    @staticmethod
    def _last_user_index(messages: Sequence[dict]) -> int:
        for index in range(len(messages) - 1, -1, -1):
            if messages[index].get("role") == "user":
                return index
        return len(messages)

    @staticmethod
    def _summary_message(record: dict) -> dict:
        summary = record.get("summary") or ""
        if not summary and record.get("summary_ref"):
            summary = f"[earlier conversation summary stored at {record['summary_ref']}]"
        return {
            "role": "system",
            "content": (
                "[Earlier conversation summary — messages "
                f"{record.get('from_index')}:{record.get('to_index')}]\n{summary}"
            ).rstrip(),
        }
