"""Pure event → view projection (§3.4 / §5.3).

``project(event, state) -> (frames, new_state)`` is the **only** place that
decides what a front-end sees.  Live streaming, history replay, reconnection and
the CLI/TUI/Web renderers all call this function, so the same event log always
yields the same frame sequence.

Hard rules (they are what makes the projector safe to reuse everywhere):

* no clock, no randomness, no I/O, no ``AgentState`` access — the input is one
  event plus the derived view state;
* ``*.delta`` events become ``ephemeral`` frames only (I4 / I23); they never
  mutate transcript state;
* the view is a *derivation*, never a second reducer: it never invents a fact
  the event log does not contain.

``phase`` is derived from events rather than read from ``AgentState`` because
``AWAITING_APPROVAL`` is not driven by the reducer today (§6.7.5): the gateway
view is the authoritative "waiting for a human" signal.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from typing import Iterable, Optional

from ..runtime.state import phases

#: Maximum bytes of a partial streaming text kept in the snapshot (§B.1).
STREAMING_LIMIT = 4 * 1024
#: Maximum characters of tool args / output inlined into a view frame (§6.9).
PREVIEW_LIMIT = 2 * 1024

#: Phase names used by the view.  A handful of states exist only in the view.
VIEW_AWAITING_APPROVAL = phases.AWAITING_APPROVAL


@dataclass(frozen=True, slots=True)
class ProjectedFrame:
    """One semantic frame produced by an event (seq/ordinal are added later)."""

    name: str
    payload: dict
    ephemeral: bool = False


@dataclass(frozen=True, slots=True)
class ProjectorState:
    """The derived view state — small, JSON-able and deterministic."""

    phase: str = phases.IDLE
    round: int = 0
    last_seq: int = 0
    busy: bool = False
    pending: tuple = ()                    # unfinished tool call ids
    pending_approvals: tuple = ()          # ((call_id, approval_info), ...)
    streaming: Optional[dict] = None
    usage: dict = field(
        default_factory=lambda: {
            "input_tokens": 0, "output_tokens": 0, "tokens": 0,
            "cache_read": 0, "cache_write": 0, "usd": 0.0,
        }
    )
    bound_model: Optional[str] = None
    context_digest: Optional[str] = None
    last_error: Optional[dict] = None
    tool_names: tuple = ()                 # ((call_id, name), ...)
    #: Current mode summary (``name``/``source``/``digest``/``refresh``).
    mode: Optional[dict] = None
    #: Request ids whose final ``llm.*`` event has already been projected.  A
    #: coalesced delta may be committed *after* its final event (the bus assigns
    #: ``seq`` at commit time), so late deltas must be dropped rather than
    #: re-opening ``streaming`` after the authoritative message.
    completed_requests: tuple = ()

    # -- convenient read-only views ---------------------------------------
    @property
    def pending_approval_ids(self) -> frozenset:
        return frozenset(call_id for call_id, _ in self.pending_approvals)

    @property
    def pending_tool_ids(self) -> frozenset:
        return frozenset(self.pending)

    def tool_name(self, call_id: str) -> Optional[str]:
        return dict(self.tool_names).get(call_id)

    def approval(self, call_id: str) -> Optional[dict]:
        return dict(self.pending_approvals).get(call_id)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _busy(phase: str, pending: set, approvals: dict, streaming: Optional[dict]) -> bool:
    return bool(
        phase in (phases.THINKING, phases.ACTING, phases.AWAITING_TOOLS,
                  phases.AWAITING_APPROVAL)
        or pending or approvals or streaming
    )


def _preview_text(text: str, limit: int = PREVIEW_LIMIT) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "…"


def _preview_value(value, limit: int = PREVIEW_LIMIT):
    """Keep small JSON inline; replace an oversized value by a preview dict."""
    try:
        encoded = json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return {"truncated": True, "preview": _preview_text(str(value), limit)}
    if len(encoded) <= limit:
        return value
    return {"truncated": True, "preview": encoded[:limit] + "…"}


def _append_partial(current: Optional[str], chunk: str, *,
                    limit: int = STREAMING_LIMIT) -> tuple[str, bool]:
    text = f"{current or ''}{chunk}"
    if len(text) <= limit:
        return text, False
    return text[-limit:], True


def _usage_add(usage: dict, payload: dict) -> dict:
    reported = payload.get("usage") or {}
    cost = payload.get("cost") or {}
    input_tokens = int(reported.get("input_tokens", 0) or 0)
    output_tokens = int(reported.get("output_tokens", 0) or 0)
    return {
        "input_tokens": int(usage.get("input_tokens", 0)) + input_tokens,
        "output_tokens": int(usage.get("output_tokens", 0)) + output_tokens,
        "tokens": int(usage.get("tokens", 0)) + input_tokens + output_tokens,
        "cache_read": int(usage.get("cache_read", 0)) + int(reported.get("cache_read", 0) or 0),
        "cache_write": int(usage.get("cache_write", 0)) + int(reported.get("cache_write", 0) or 0),
        "usd": round(float(usage.get("usd", 0.0)) + float(cost.get("total_usd", 0.0) or 0.0), 8),
    }


def _as_error_info(payload: dict) -> Optional[dict]:
    info = payload.get("error_info")
    if isinstance(info, dict):
        return info
    if payload.get("code") or payload.get("error"):
        return {
            "code": payload.get("code") or "E_UNKNOWN",
            "source": payload.get("source") or "unknown",
            "message": payload.get("error") or "",
            "detail": payload.get("error") or "",
            "retryable": False,
            "enters_history": False,
            "context": {},
        }
    return None


# ---------------------------------------------------------------------------
# projector
# ---------------------------------------------------------------------------

def project(event, state: ProjectorState) -> tuple[list[ProjectedFrame], ProjectorState]:
    """Derive view frames for one committed event (pure)."""
    etype = event.type
    payload = event.payload if isinstance(event.payload, dict) else {}
    frames: list[ProjectedFrame] = []

    phase = state.phase
    round_number = state.round
    pending = set(state.pending)
    approvals = dict(state.pending_approvals)
    tool_names = dict(state.tool_names)
    streaming = dict(state.streaming) if state.streaming is not None else None
    usage = dict(state.usage)
    bound_model = state.bound_model
    context_digest = state.context_digest
    last_error = state.last_error
    mode = dict(state.mode) if state.mode is not None else None
    completed = list(state.completed_requests)

    def _complete_request(request_id: Optional[str]) -> None:
        if request_id and request_id not in completed:
            completed.append(request_id)
            del completed[:-32]  # bounded diagnostic/ordering cache

    # -- user domain ------------------------------------------------------
    if etype == "user.message":
        frames.append(ProjectedFrame("user.message", {
            "text": payload.get("text", ""),
            "client_message_id": payload.get("client_message_id"),
            "client_id": payload.get("client_id"),
            "event_id": event.id,
            "trace_id": event.trace_id,
        }))
        phase = phases.THINKING
        streaming = None
        last_error = None

    elif etype == "user.interrupt":
        frames.append(ProjectedFrame("notice", {"kind": "interrupt"}))
        phase = phases.IDLE
        streaming = None
        pending.clear()
        approvals.clear()
        tool_names.clear()

    # -- LLM domain -------------------------------------------------------
    elif etype == "llm.started":
        request_id = event.cause_id or event.id
        frames.append(ProjectedFrame("assistant.started", {
            "provider": payload.get("provider"),
            "model": payload.get("model"),
            "alias": payload.get("alias"),
            "round": payload.get("round"),
            "context_digest": payload.get("context_digest"),
        }))
        phase = phases.THINKING
        streaming = {
            "request_id": request_id,
            "kind": "text",
            "partial_text": "",
            "truncated": False,
        }
        context_digest = payload.get("context_digest") or context_digest
        last_error = None
        if payload.get("round") is not None:
            round_number = int(payload["round"])

    elif etype in ("llm.token.delta", "llm.reasoning.delta"):
        if event.cause_id in completed:
            pass  # late coalesced delta: the final message is already authoritative
        else:
            kind = "text" if etype == "llm.token.delta" else "reasoning"
            name = "assistant.delta" if kind == "text" else "reasoning.delta"
            text = payload.get("text", "") or ""
            frames.append(ProjectedFrame(name, {
                "text": text,
                "request_id": event.cause_id,
            }, ephemeral=True))
            if streaming is None:
                streaming = {
                    "request_id": event.cause_id,
                    "kind": kind,
                    "partial_text": "",
                    "truncated": False,
                }
            partial, truncated = _append_partial(streaming.get("partial_text"), text)
            streaming.update({
                "request_id": event.cause_id or streaming.get("request_id"),
                "kind": kind,
                "partial_text": partial,
                "truncated": bool(streaming.get("truncated") or truncated),
            })

    elif etype == "llm.completed":
        calls = payload.get("tool_calls") or []
        message = payload.get("message") or {}
        interrupted = bool(payload.get("interrupted"))
        frames.append(ProjectedFrame("assistant.message", {
            # The final message is authoritative and carries no output_ref, so
            # it is never truncated here (the bus already bounds payload size).
            "message": message,
            "tool_calls": calls,
            "finish": payload.get("finish"),
            "usage": payload.get("usage") or {},
            "cost": payload.get("cost") or {},
            "provider": payload.get("provider"),
            "model_actual": payload.get("model_actual"),
            "round": payload.get("round"),
            "interrupted": interrupted,
        }))
        streaming = None
        last_error = None
        _complete_request(event.cause_id)
        usage = _usage_add(usage, payload)
        provider = payload.get("provider")
        actual = payload.get("model_actual")
        if provider and actual:
            bound_model = f"{provider}/{actual}"
        if payload.get("round") is not None:
            round_number = int(payload["round"])
        if interrupted:
            phase = phases.IDLE
            pending.clear()
        elif calls:
            phase = phases.AWAITING_TOOLS
            for call in calls:
                call_id = call.get("call_id") if isinstance(call, dict) else None
                if call_id:
                    pending.add(call_id)
                    tool_names[call_id] = call.get("name", "")
        else:
            phase = phases.IDLE
            pending.clear()

    elif etype == "llm.failed":
        info = _as_error_info(payload)
        frames.append(ProjectedFrame("error", {
            "error_info": info,
            "code": payload.get("code"),
            "round": payload.get("round"),
        }))
        phase = phases.IDLE
        streaming = None
        pending.clear()
        last_error = info
        _complete_request(event.cause_id)
        if payload.get("round") is not None:
            round_number = int(payload["round"])

    # -- tool domain ------------------------------------------------------
    elif etype == "tool.call.requested":
        call_id = payload.get("call_id")
        name = payload.get("name")
        frames.append(ProjectedFrame("tool.requested", {
            "call_id": call_id,
            "name": name,
            "args": _preview_value(payload.get("args") or {}),
        }))
        if call_id:
            pending.add(call_id)
            tool_names[call_id] = name
        phase = phases.AWAITING_TOOLS

    elif etype == "tool.call.started":
        frames.append(ProjectedFrame("tool.started", {
            "call_id": payload.get("call_id"),
            "name": payload.get("name"),
            "side_effect": payload.get("side_effect"),
        }))

    elif etype == "tool.output.delta":
        if payload.get("call_id") in pending:
            frames.append(ProjectedFrame("tool.delta", {
                "call_id": payload.get("call_id"),
                "text": _preview_text(payload.get("text", "") or ""),
            }, ephemeral=True))
        # else: a coalesced delta after the authoritative tool result — drop it.

    elif etype in ("tool.call.completed", "tool.call.failed"):
        call_id = payload.get("call_id")
        ok = etype == "tool.call.completed" and bool(payload.get("ok", True))
        output = payload.get("output")
        if payload.get("output_ref"):
            # The full text is available through ``artifact.read``; only a
            # preview travels inline (§6.9).
            output = _preview_text(output if isinstance(output, str) else str(output))
        elif not isinstance(output, str):
            output = _preview_value(output)
        frames.append(ProjectedFrame("tool.result", {
            "call_id": call_id,
            "name": payload.get("name") or tool_names.get(call_id),
            "ok": ok,
            "output": output,
            "output_ref": payload.get("output_ref"),
            "output_tokens": payload.get("output_tokens"),
            "dedup": bool(payload.get("dedup", False)),
            "code": payload.get("code"),
            "error": payload.get("error"),
            "error_info": _as_error_info(payload),
        }))
        pending.discard(call_id)
        tool_names.pop(call_id, None)
        approvals.pop(call_id, None)
        if not ok:
            last_error = _as_error_info(payload)
        # Every branch done -> the reducer asks the model again.
        phase = phases.THINKING if not pending else phases.AWAITING_TOOLS

    # -- approval domain --------------------------------------------------
    elif etype == "approval.required":
        call_id = payload.get("call_id")
        info = {
            "call_id": call_id,
            "name": payload.get("name") or tool_names.get(call_id),
            "side_effect": payload.get("side_effect"),
            "args_preview": payload.get("args_preview"),
            "requested_at": event.ts,
            "expires_at": payload.get("expires_at"),
        }
        frames.append(ProjectedFrame("approval.required", info))
        if call_id:
            approvals[call_id] = info
        phase = phases.AWAITING_APPROVAL

    elif etype in ("approval.granted", "approval.denied"):
        call_id = payload.get("call_id")
        decision = "granted" if etype == "approval.granted" else "denied"
        frames.append(ProjectedFrame("approval.resolved", {
            "call_id": call_id,
            "decision": decision,
            "reason": payload.get("reason"),
            "client_id": payload.get("client_id"),
        }))
        approvals.pop(call_id, None)
        phase = phases.AWAITING_TOOLS

    # -- agent lifecycle --------------------------------------------------
    elif etype == "agent.paused":
        if payload.get("reason") == "approval":
            phase = phases.AWAITING_APPROVAL
        else:
            phase = phases.PAUSED

    elif etype == "agent.resumed":
        phase = phases.THINKING
        last_error = None

    elif etype == "agent.completed":
        info = _as_error_info(payload)
        frames.append(ProjectedFrame("agent.completed", {
            "round": payload.get("round", round_number),
            "finish": payload.get("finish"),
            "code": payload.get("code"),
            "error_info": info,
            "recovered": bool(payload.get("recovered", False)),
        }))
        phase = phases.IDLE
        streaming = None
        pending.clear()
        if payload.get("finish") == "error":
            last_error = info
        if payload.get("round") is not None:
            round_number = int(payload["round"])

    elif etype == "tool.cancel.requested":
        call_id = payload.get("call_id")
        if call_id:
            pending.discard(call_id)
            approvals.pop(call_id, None)
            tool_names.pop(call_id, None)
        else:
            pending.clear()
            approvals.clear()
            tool_names.clear()
        frames.append(ProjectedFrame("notice", {
            "kind": "interrupt", "call_id": call_id,
        }))
        phase = phases.IDLE
        streaming = None

    # -- subagent domain --------------------------------------------------
    elif etype == "subagent.spawn.requested":
        call_id = payload.get("call_id")
        frames.append(ProjectedFrame("subagent.requested", {
            "call_id": call_id,
            "title": payload.get("title"),
            "task": _preview_text(payload.get("task", "") or ""),
            "tools": list(payload.get("tools") or ()),
            "model_profile": payload.get("model_profile"),
        }))
        if call_id:
            pending.add(call_id)
            tool_names[call_id] = "spawn_subagent"
        phase = phases.AWAITING_TOOLS

    elif etype == "subagent.started":
        frames.append(ProjectedFrame("subagent.started", {
            "call_id": payload.get("call_id"),
            "child_session_id": payload.get("child_session_id"),
            "parent_session_id": payload.get("parent_session_id"),
            "title": payload.get("title"),
            "tools": list(payload.get("tools") or ()),
            "model_profile": payload.get("model_profile"),
            "read_only": bool(payload.get("read_only", False)),
            "approval_policy": payload.get("approval_policy"),
            "timeout_ms": payload.get("timeout_ms"),
            "timeout_clamped": bool(payload.get("timeout_clamped", False)),
        }))

    elif etype == "subagent.progress":
        frames.append(ProjectedFrame("subagent.progress", {
            "call_id": payload.get("call_id"),
            "child_session_id": payload.get("child_session_id"),
            "title": payload.get("title"),
            "phase": payload.get("phase"),
            "round": payload.get("round"),
            "last_tool": payload.get("last_tool"),
            "message_count": payload.get("message_count"),
            "tokens_input": payload.get("tokens_input"),
            "tokens_output": payload.get("tokens_output"),
        }))

    elif etype == "subagent.completed":
        call_id = payload.get("call_id")
        was_pending = call_id is not None and call_id in pending
        frames.append(ProjectedFrame("subagent.result", {
            "call_id": call_id,
            "child_session_id": payload.get("child_session_id"),
            "title": payload.get("title"),
            "ok": True,
            "finish": payload.get("finish", "completed"),
            "summary": _preview_text(payload.get("summary", "") or ""),
            "summary_ref": payload.get("summary_ref"),
            "summary_truncated": bool(payload.get("summary_truncated", False)),
            "usage": payload.get("usage") or {},
            "rounds": payload.get("rounds"),
            "duration_ms": payload.get("duration_ms"),
            "model_profile": payload.get("model_profile"),
        }))
        pending.discard(call_id)
        tool_names.pop(call_id, None)
        approvals.pop(call_id, None)
        if was_pending:
            phase = phases.THINKING if not pending else phases.AWAITING_TOOLS

    elif etype == "subagent.failed":
        call_id = payload.get("call_id")
        was_pending = call_id is not None and call_id in pending
        info = _as_error_info(payload)
        frames.append(ProjectedFrame("subagent.error", {
            "call_id": call_id,
            "child_session_id": payload.get("child_session_id"),
            "title": payload.get("title"),
            "ok": False,
            "finish": payload.get("finish", "error"),
            "code": payload.get("code"),
            "error": payload.get("error"),
            "error_info": info,
            "usage": payload.get("usage") or {},
            "rounds": payload.get("rounds"),
            "duration_ms": payload.get("duration_ms"),
        }))
        pending.discard(call_id)
        tool_names.pop(call_id, None)
        approvals.pop(call_id, None)
        last_error = info or last_error
        if was_pending:
            phase = phases.THINKING if not pending else phases.AWAITING_TOOLS

    elif etype == "subagent.cancel.requested":
        call_ids = list(payload.get("call_ids") or ())
        if call_ids:
            for call_id in call_ids:
                pending.discard(call_id)
                tool_names.pop(call_id, None)
                approvals.pop(call_id, None)
        else:
            for call_id, name in list(tool_names.items()):
                if name == "spawn_subagent":
                    pending.discard(call_id)
                    tool_names.pop(call_id, None)
        frames.append(ProjectedFrame("notice", {
            "kind": "subagent_cancel", "call_ids": call_ids,
        }))
        if not pending:
            phase = phases.IDLE

    # -- notices / compaction / plugins ----------------------------------
    elif etype == "system.budget.exceeded":
        info = _as_error_info(payload)
        frames.append(ProjectedFrame("notice", {
            "kind": "budget",
            "reason": payload.get("reason"),
            "code": payload.get("code", "E_BUDGET"),
            "error_info": info,
        }))
        phase = phases.PAUSED
        streaming = None
        last_error = info

    elif etype in ("system.provider.degraded", "system.provider.quarantined"):
        frames.append(ProjectedFrame("notice", {
            "kind": "provider",
            "status": etype.rsplit(".", 1)[-1],
            "provider": payload.get("provider"),
            "model": payload.get("model"),
        }))

    elif etype == "llm.params.dropped":
        frames.append(ProjectedFrame("notice", {
            "kind": "params_dropped",
            "keys": payload.get("keys") or [],
            "provider": payload.get("provider"),
            "model": payload.get("model"),
        }))

    elif etype in ("llm.warnings", "llm.context.transplanted"):
        kind = "warnings" if etype == "llm.warnings" else "context_transplanted"
        frames.append(ProjectedFrame("notice", {
            "kind": kind,
            **{key: value for key, value in payload.items()
               if key in ("warnings", "reason", "provider", "model", "from", "to")},
        }))

    elif etype == "context.compaction.requested":
        frames.append(ProjectedFrame("compaction.started", {
            "from_index": payload.get("from_index"),
            "to_index": payload.get("to_index"),
            "target_tokens": payload.get("target_tokens"),
            "source_digest": payload.get("source_digest"),
        }))

    elif etype == "context.compaction.applied":
        frames.append(ProjectedFrame("compaction.applied", {
            "from_index": payload.get("from_index"),
            "to_index": payload.get("to_index"),
            "summary": _preview_text(payload.get("summary", "") or ""),
            "summary_ref": payload.get("summary_ref"),
            "tokens": payload.get("tokens"),
            "summarizer": payload.get("summarizer"),
            "fallback": bool(payload.get("fallback", False)),
            "error_info": _as_error_info(payload),
        }))
        phase = phases.THINKING

    elif etype.startswith("system.plugin."):
        frames.append(ProjectedFrame("notice", {
            "kind": "plugin",
            "status": etype.rsplit(".", 1)[-1],
            "plugin_id": payload.get("plugin_id") or payload.get("id"),
        }))

    elif etype == "system.mode.changed":
        mode = {
            "id": f"{payload.get('source')}/{payload.get('to')}",
            "name": payload.get("to"),
            "source": payload.get("source"),
            "digest": payload.get("to_digest"),
            "from": payload.get("from"),
            "refresh": bool(payload.get("refresh", False)),
        }
        frames.append(ProjectedFrame("mode", dict(mode)))

    elif etype in ("memory.write.applied", "memory.deleted", "system.session.erased"):
        kind = {
            "memory.write.applied": "memory.write.applied",
            "memory.deleted": "memory.deleted",
            "system.session.erased": "session.erased",
        }[etype]
        frames.append(ProjectedFrame("notice", {"kind": kind}))

    # Everything else (internal intents, retrieval/context events) is visible
    # only in ``events`` mode; the view deliberately ignores it.

    last_seq = max(state.last_seq, int(event.seq or 0))
    new_state = replace(
        state,
        phase=phase,
        round=round_number,
        last_seq=last_seq,
        busy=_busy(phase, pending, approvals, streaming),
        pending=tuple(sorted(pending)),
        pending_approvals=tuple(approvals.items()),
        streaming=streaming,
        usage=usage,
        bound_model=bound_model,
        context_digest=context_digest,
        last_error=last_error,
        tool_names=tuple(tool_names.items()),
        mode=mode,
        completed_requests=tuple(completed),
    )
    if new_state.phase != state.phase:
        frames.append(ProjectedFrame("phase.changed", {
            "from": state.phase,
            "to": new_state.phase,
        }))
    return frames, new_state


def project_all(
    events: Iterable, state: Optional[ProjectorState] = None
) -> tuple[list[ProjectedFrame], ProjectorState]:
    """Project a sequence of events (used by history/replay paths)."""
    current = state or ProjectorState()
    frames: list[ProjectedFrame] = []
    for event in events:
        produced, current = project(event, current)
        frames.extend(produced)
    return frames, current


def snapshot_payload(state: ProjectorState) -> dict:
    """The ``snapshot`` frame payload (Appendix B.1)."""
    return {
        "last_seq": state.last_seq,
        "phase": state.phase,
        "round": state.round,
        "busy": state.busy,
        "pending": list(state.pending),
        "streaming": dict(state.streaming) if state.streaming is not None else None,
        "pending_approvals": [dict(info) for _, info in state.pending_approvals],
        "usage": dict(state.usage),
        "bound_model": state.bound_model,
        "mode": dict(state.mode) if state.mode is not None else None,
        "context_digest": state.context_digest,
        "last_error": dict(state.last_error) if state.last_error is not None else None,
    }


__all__ = [
    "PREVIEW_LIMIT",
    "ProjectedFrame",
    "ProjectorState",
    "STREAMING_LIMIT",
    "VIEW_AWAITING_APPROVAL",
    "project",
    "project_all",
    "snapshot_payload",
]
