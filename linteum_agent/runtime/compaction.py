"""Deterministic, event-driven context compaction (§29–§31).

The agent never calls a summariser.  When the derived context exceeds its budget
it publishes ``context.compaction.requested``; this module's
:class:`ContextCompactor` turns that intent into a summary and publishes
``context.compaction.applied``.  The summary is recorded in the WAL, so a replay
re-applies the recorded result instead of calling a model (P13).

The default summariser is deterministic and dependency-free; hosts can inject an
LLM-backed :class:`Summarizer` without changing the event flow.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Optional, Protocol, Sequence

from ..bus.artifact import ARTIFACT_PREFIX, ArtifactStore, NullArtifactStore, hydrate_event
from .errors import ErrorSource, error_info
from .llm.canonical import estimate_tokens

DEFAULT_SUMMARY_TOKENS = 768
DEFAULT_INLINE_SUMMARY = 8 * 1024


@dataclass(frozen=True, slots=True)
class SummaryResult:
    text: str
    tokens: int
    summarizer: str = "deterministic"
    fallback: bool = False


class Summarizer(Protocol):
    async def summarize(
        self, messages: Sequence[dict], *, target_tokens: int, session_id: str
    ) -> SummaryResult: ...


def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if not isinstance(part, dict):
                parts.append(str(part))
            elif part.get("type") == "text":
                parts.append(str(part.get("text", "")))
            elif part.get("ref"):
                parts.append(f"[{part.get('type', 'attachment')}: {part['ref']}]")
        return "".join(parts)
    return "" if content is None else str(content)


def _render_message(message: dict, *, limit: int) -> str:
    role = message.get("role", "?")
    calls = message.get("tool_calls") or []
    if message.get("tool_call_id") is not None:
        role = f"tool({message['tool_call_id']})"
    text = _text_of(message.get("content", ""))
    if calls:
        rendered = ", ".join(
            f"{call.get('name', '?')}({json.dumps(call.get('args') or {}, ensure_ascii=False)})"
            for call in calls if isinstance(call, dict)
        )
        text = f"{text} [tool_calls: {rendered}]".strip()
    text = " ".join(text.split())
    if len(text) > limit:
        text = text[:limit] + "…"
    return f"{role}: {text}"


class DeterministicSummarizer:
    """Token-bounded, deterministic transcript folding (no clock/random/network)."""

    def __init__(self, *, per_message_chars: int = 400) -> None:
        self.per_message_chars = max(40, int(per_message_chars))

    async def summarize(
        self, messages: Sequence[dict], *, target_tokens: int, session_id: str
    ) -> SummaryResult:
        del session_id
        target = max(1, int(target_tokens))
        budget_chars = max(80, target * 4)
        header = f"[Earlier conversation: {len(messages)} message(s) compacted]"
        lines = [header]
        used = len(header)
        omitted = 0
        for message in messages:
            rendered = _render_message(message, limit=self.per_message_chars)
            remaining = budget_chars - used
            if remaining <= 0:
                omitted += 1
                continue
            if len(rendered) > remaining:
                if remaining > 20:
                    lines.append(rendered[: remaining - 1] + "…")
                    used += remaining
                omitted += 1
                continue
            lines.append(rendered)
            used += len(rendered) + 1
        if omitted:
            lines.append(f"[{omitted} message(s) omitted for brevity]")
        text = "\n".join(lines)
        return SummaryResult(text=text, tokens=estimate_tokens(text))


class ContextCompactor:
    """The only subscriber to ``context.compaction.requested``."""

    def __init__(
        self,
        bus,
        *,
        summarizer: Optional[Summarizer] = None,
        artifacts: Optional[ArtifactStore] = None,
        inline_limit: int = DEFAULT_INLINE_SUMMARY,
        auto_subscribe: bool = True,
    ) -> None:
        self.bus = bus
        self.summarizer = summarizer or DeterministicSummarizer()
        chosen = artifacts if artifacts is not None else getattr(bus, "artifacts", None)
        self.artifacts = chosen if chosen is not None else NullArtifactStore()
        self.inline_limit = max(256, int(inline_limit))
        self._unsubscribe = None
        if auto_subscribe:
            self.subscribe()

    def subscribe(self) -> None:
        self._unsubscribe = self.bus.on(
            "context.compaction.requested", self.on_request
        )

    def close(self) -> None:
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None

    async def on_request(self, ev) -> None:
        try:
            ev = await hydrate_event(ev, self.artifacts)
        except Exception as exc:  # noqa: BLE001 - report, never strand the reducer
            failure = error_info(
                "E_ARTIFACT_MISSING",
                source=ErrorSource.CONTEXT.value,
                location="linteum_agent.runtime.compaction.ContextCompactor.on_request",
                message=f"compaction payload could not be restored: {exc}",
                exc=exc,
                retryable=True,
                enters_history=False,
            ).to_dict()
            await self.bus.publish(ev.child("context.compaction.applied", {
                "from_index": -1,
                "to_index": -1,
                "source_digest": "",
                "summary": "[context compaction failed: payload unavailable]",
                "summary_ref": None,
                "tokens": 0,
                "summarizer": "unavailable",
                "fallback": True,
                "error_info": failure,
            }))
            return

        payload = ev.payload
        messages = payload.get("messages") or []
        target = int(payload.get("target_tokens") or DEFAULT_SUMMARY_TOKENS)
        fallback = False
        failure = None
        try:
            result = await self.summarizer.summarize(
                messages, target_tokens=target, session_id=ev.session_id
            )
        except Exception as exc:  # noqa: BLE001 - compaction must never strand the agent
            fallback = True
            failure = error_info(
                "E_CONTEXT_OVERFLOW",
                source=ErrorSource.CONTEXT.value,
                location="linteum_agent.runtime.compaction.ContextCompactor.on_request",
                message=f"summarizer failed: {exc}",
                exc=exc,
                retryable=False,
                enters_history=False,
                context={"summarizer": type(self.summarizer).__name__},
            ).to_dict()
            try:
                result = await DeterministicSummarizer().summarize(
                    messages, target_tokens=target, session_id=ev.session_id
                )
            except Exception:  # pragma: no cover - deterministic fallback is total
                result = SummaryResult(
                    text="[context compaction failed; earlier messages omitted]",
                    tokens=0,
                    summarizer="unavailable",
                    fallback=True,
                )

        encoded = result.text.encode("utf-8")
        summary_ref = None
        if len(encoded) > self.inline_limit and self.artifacts.available:
            artifact = await self.artifacts.put(
                encoded,
                mime="text/plain",
                session_id=ev.session_id,
            )
            summary_ref = f"{ARTIFACT_PREFIX}{artifact.hash}"
            encoded = encoded[: self.inline_limit]
        summary = encoded.decode("utf-8", errors="ignore")

        applied = {
            "from_index": int(payload.get("from_index", 0)),
            "to_index": int(payload.get("to_index", 0)),
            "source_digest": payload.get("source_digest", ""),
            "summary": summary,
            "summary_ref": summary_ref,
            "tokens": estimate_tokens(summary),
            "summarizer": result.summarizer,
            "fallback": bool(result.fallback or fallback),
        }
        if failure is not None:
            applied["error_info"] = failure
        await self.bus.publish(ev.child("context.compaction.applied", applied))


__all__ = [
    "ContextCompactor",
    "DeterministicSummarizer",
    "Summarizer",
    "SummaryResult",
    "DEFAULT_SUMMARY_TOKENS",
]
