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

import json
from typing import Optional

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


class ApprovalGate:
    """Pending-approval bookkeeping; drives the runtime on decision."""

    def __init__(self, bus, runtime=None, *, preview_limit: int = DEFAULT_PREVIEW_LIMIT) -> None:
        self.bus = bus
        self.runtime = runtime
        self.preview_limit = preview_limit
        #: call_id -> the original ``tool.call.requested`` event (persistable).
        self.pending: dict[str, object] = {}

    async def request(self, ev, spec) -> None:
        """Suspend an approval-gated call and ask the front-end (§24)."""
        call_id = ev.payload["call_id"]
        self.pending[call_id] = ev
        await self.bus.publish(ev.child("approval.required", {
            "call_id": call_id,
            "name": spec.name,
            "side_effect": spec.side_effect,
            "args_preview": redact(ev.payload.get("args") or {}, limit=self.preview_limit),
        }))
        await self.bus.publish(ev.child("agent.paused", {
            "reason": "approval", "call_id": call_id,
        }))

    async def on_granted(self, ev) -> None:
        call_id = ev.payload.get("call_id")
        original = self.pending.pop(call_id, None)
        if original is None or self.runtime is None:
            return
        await self.runtime.resume_approved(original)

    async def on_denied(self, ev) -> None:
        call_id = ev.payload.get("call_id")
        original = self.pending.pop(call_id, None)
        if original is None or self.runtime is None:
            return
        await self.runtime.deny(original, ev.payload.get("reason", "approval denied"))

    def pending_ids(self) -> tuple:
        return tuple(self.pending)

    def __len__(self) -> int:
        return len(self.pending)
