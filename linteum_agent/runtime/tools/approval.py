"""ApprovalGate: human-in-the-loop for side-effecting tools (§20.2, §24).

A tool marked ``requires_approval`` (which is the default for ``write`` /
``dangerous`` side effects, §21) is not executed on ``tool.call.requested``.
Instead the gate:

1. records the request in ``pending`` so the session can be **persisted and the
   process restarted** while a human decides, then
2. emits ``approval.required`` (with a redacted + truncated ``args_preview`` for
   the UI) followed by ``agent.paused``.

When ``approval.granted`` arrives the original call is replayed into the runtime;
``approval.denied`` resolves it as ``tool.call.failed{E_DENIED}`` so the agent's
convergence barrier still advances (I3 — every request is paired).

The preview is **always** redacted and truncated: it is rendered in an approval
dialog and may end up in logs (§24).
"""

from __future__ import annotations

import inspect
import json
from dataclasses import dataclass
from typing import Callable, Optional

#: Substrings that mark a key as sensitive (case-insensitive) for the preview.
SECRET_HINTS: tuple = (
    "key", "token", "secret", "password", "passwd", "credential",
    "authorization", "auth", "cookie",
)

DEFAULT_PREVIEW_LIMIT = 240


def _looks_secret(key: object) -> bool:
    if not isinstance(key, str):
        return False
    lowered = key.lower()
    return any(hint in lowered for hint in SECRET_HINTS)


async def _maybe_await(value) -> None:
    if inspect.isawaitable(value):
        await value


def redact(value, *, limit: int = DEFAULT_PREVIEW_LIMIT) -> str:
    """Render ``value`` as JSON with secret keys masked and the text truncated."""

    def scrub(node):
        if isinstance(node, dict):
            return {k: ("***" if _looks_secret(k) else scrub(v)) for k, v in node.items()}
        if isinstance(node, (list, tuple)):
            return [scrub(v) for v in node]
        return node

    try:
        text = json.dumps(scrub(value), ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = str(value)
    return text if len(text) <= limit else text[:limit] + "…"


@dataclass
class _PendingApproval:
    """One suspended request plus the callbacks that resolve it.

    ``None`` callbacks mean the historical behaviour: resume through
    ``ToolRuntime.resume_approved`` / deny through ``ToolRuntime.deny``.  A
    background ``start`` handshake registers manager-owned callbacks instead of
    turning into a ``tool.call.requested`` execution (§8.4 / §9.6).
    """

    event: object
    resume: Optional[Callable] = None
    deny: Optional[Callable] = None

    @property
    def session_id(self) -> Optional[str]:
        return getattr(self.event, "session_id", None)

    @property
    def type(self) -> str:
        return getattr(self.event, "type", "")


class ApprovalGate:
    """Pending-approval bookkeeping; drives the runtime on decision."""

    def __init__(self, bus, runtime=None, *, preview_limit: int = DEFAULT_PREVIEW_LIMIT) -> None:
        self.bus = bus
        self.runtime = runtime
        self.preview_limit = preview_limit
        #: call_id -> :class:`_PendingApproval` (original event + callbacks).
        self.pending: dict[str, _PendingApproval] = {}

    async def request(self, ev, spec, *, resume=None, deny=None) -> None:
        """Suspend an approval-gated call and ask the front-end (§24).

        ``resume`` / ``deny`` let the background manager own the post-decision
        handshake; the default foreground path is unchanged.
        """
        call_id = ev.payload["call_id"]
        self.pending[call_id] = _PendingApproval(ev, resume=resume, deny=deny)
        payload = {
            "call_id": call_id,
            "name": spec.name,
            "side_effect": spec.side_effect,
            "args_preview": redact(ev.payload.get("args") or {}, limit=self.preview_limit),
        }
        if ev.type == "background.start.requested":
            # The UI can render "approve background command" (§9.6).
            payload["kind"] = "background_start"
            payload["tool"] = ev.payload.get("tool")
        await self.bus.publish(ev.child("approval.required", payload))
        await self.bus.publish(ev.child("agent.paused", {
            "reason": "approval", "call_id": call_id,
        }))

    async def on_granted(self, ev) -> None:
        call_id = ev.payload.get("call_id")
        pending = self.pending.pop(call_id, None)
        if pending is None:
            return
        # Accept a raw ``Event`` too: tests/hosts may seed ``pending`` directly
        # (the historical shape), and both must resolve identically.
        original = getattr(pending, "event", pending)
        resume = getattr(pending, "resume", None)
        if resume is not None:
            await _maybe_await(resume(original))
            return
        if getattr(original, "type", "") == "background.start.requested":
            return  # no manager callback registered: never execute a stray start
        if self.runtime is None:
            return
        await self.runtime.resume_approved(original)

    async def on_denied(self, ev) -> None:
        call_id = ev.payload.get("call_id")
        pending = self.pending.pop(call_id, None)
        if pending is None:
            return
        original = getattr(pending, "event", pending)
        reason = ev.payload.get("reason", "approval denied")
        deny = getattr(pending, "deny", None)
        if deny is not None:
            await _maybe_await(deny(original, reason))
            return
        if getattr(original, "type", "") == "background.start.requested":
            return
        if self.runtime is None:
            return
        await self.runtime.deny(original, reason)

    # -- invalidation (§6.7 / §7.4) ----------------------------------------
    def cancel(self, *, call_id: Optional[str] = None,
               session_id: Optional[str] = None) -> list[str]:
        """Drop pending approvals so a late decision cannot execute a tool.

        Called when ``tool.cancel.requested`` is reduced.  Without it the
        reducer voids the tool result while the gate still holds the original
        request — a later ``approval.granted`` would then run a side-effecting
        tool whose outcome is discarded (the §6.7 race).
        """
        removed: list[str] = []
        for pending_id, pending in list(self.pending.items()):
            if call_id is not None and pending_id != call_id:
                continue
            entry = getattr(pending, "event", pending)
            if session_id is not None and getattr(entry, "session_id", None) != session_id:
                continue
            self.pending.pop(pending_id, None)
            removed.append(pending_id)
        return removed

    # -- recovery (§6.7 / §7.5) --------------------------------------------
    def restore(self, session_id: str, events) -> dict:
        """Rebuild this session's pending approvals from committed events.

        ``approval.required`` opens a call and ``approval.granted`` /
        ``approval.denied`` / the paired ``tool.call.*`` result closes it.  A
        background ``background.start.requested`` opens a start handshake and
        ``background.started`` / ``background.start.failed`` closes it.  A
        session-wide ``tool.cancel.requested`` closes everything requested so
        far.  The original request event is retained so a post-restart grant can
        still resume it.  Returns the restored ``call_id -> event`` map for
        *this* session.
        """
        requested: dict[str, object] = {}
        required: set[str] = set()
        closed: set[str] = set()
        for ev in events:
            if ev.session_id != session_id:
                continue
            payload = ev.payload if isinstance(ev.payload, dict) else {}
            call_id = payload.get("call_id")
            if ev.type in (
                "tool.call.requested", "background.start.requested",
            ) and call_id:
                # A provider may reuse a call id in a later round; the newest
                # request opens a fresh lifecycle for that id.
                requested[call_id] = ev
                required.discard(call_id)
                closed.discard(call_id)
            elif ev.type == "approval.required" and call_id:
                required.add(call_id)
                closed.discard(call_id)
            elif ev.type in (
                "approval.granted", "approval.denied",
                "tool.call.completed", "tool.call.failed",
                "background.started", "background.start.failed",
            ) and call_id:
                closed.add(call_id)
            elif ev.type == "background.cancel.requested":
                mentioned = list(payload.get("call_ids") or ())
                if call_id:
                    mentioned.append(call_id)
                closed.update(mentioned)
            elif ev.type == "tool.cancel.requested":
                if call_id:
                    closed.add(call_id)
                else:
                    closed.update(requested)

        restored: dict[str, object] = {}
        for call_id in required - closed:
            original = requested.get(call_id)
            if original is not None:
                self.pending[call_id] = _PendingApproval(original)
                restored[call_id] = original
        return restored

    def pending_ids(self, session_id: Optional[str] = None) -> tuple:
        """Pending call ids, optionally restricted to one session."""
        if session_id is None:
            return tuple(self.pending)
        return tuple(
            call_id for call_id, pending in self.pending.items()
            if getattr(getattr(pending, "event", pending), "session_id", None)
            == session_id
        )

    def __len__(self) -> int:
        return len(self.pending)
