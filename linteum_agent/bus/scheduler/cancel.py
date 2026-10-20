"""Cancellation primitives: tokens, registry and the ``Cancelled`` signal (§16.3).

Cancellation in this framework is **cooperative at the token level and hard at the
task level** (§16.3):

* a :class:`CancelToken` is a one-way flag every long-lived worker may check with
  :meth:`~CancelToken.raise_if_cancelled` (or ``await token.wait()``);
* :meth:`CancelToken.cancel` trips it, and callers that own an ``asyncio.Task``
  for the same unit of work additionally call ``task.cancel()`` (I19 — a worker
  must never *swallow* ``CancelledError``);
* a :class:`CancelRegistry` maps a ``session_id`` (optionally a ``call_id``) to
  its token so the scheduler can cancel a whole session or a single tool call.

Values are deliberately protocol-shaped: the LLM executor only needs
``registry.token_for(session_id)`` (§16.3), and the tool runtime needs a fresh
token per ``call_id``.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import Optional


class Cancelled(Exception):
    """Raised inside a worker whose :class:`CancelToken` has been tripped.

    Distinct from :class:`asyncio.CancelledError` on purpose: the tool runtime
    turns this into a ``tool.call.failed{E_CANCELLED}`` event (P4), whereas an
    ``asyncio.CancelledError`` must be re-raised untouched (I19).
    """


class CancelToken:
    """A one-way, awaitable cancellation flag.

    Usable as a plain flag (``token.raise_if_cancelled()``) or as an async
    context manager (``async with token: ...``), which is how the sandbox wraps
    a worker so that a pre-tripped token still refuses to start.  Entering and
    leaving the context is a no-op — cancellation is observed cooperatively at
    the next check point.
    """

    __slots__ = ("_event", "_cancelled")

    def __init__(self) -> None:
        self._event = asyncio.Event()
        self._cancelled = False

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    def cancel(self) -> None:
        """Trip the token; idempotent."""
        self._cancelled = True
        self._event.set()

    def raise_if_cancelled(self) -> None:
        """Raise :class:`Cancelled` when the token has been tripped."""
        if self._cancelled:
            raise Cancelled("cancelled")

    async def wait(self) -> None:
        """Block until the token is tripped (used to race a worker)."""
        await self._event.wait()

    async def __aenter__(self) -> "CancelToken":
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


class CancelRegistry:
    """Session- and call-scoped token registry (§16.3 / §53).

    ``token_for`` is memoised so a repeated cancel targets the same object; the
    registry is the canonical owner and callers request tokens through it rather
    than constructing their own when they need cross-component reach.
    """

    def __init__(self) -> None:
        self._tokens: dict[str, dict[str, CancelToken]] = defaultdict(dict)

    def token_for(self, session_id: str, call_id: str = "*") -> CancelToken:
        """Return (creating if needed) the token for ``(session_id, call_id)``."""
        return self._tokens[session_id].setdefault(call_id, CancelToken())

    def register(self, session_id: str, call_id: str, token: CancelToken) -> None:
        """Adopt an externally created token (e.g. the tool runtime's)."""
        self._tokens[session_id][call_id] = token

    def forget(self, session_id: str, call_id: str = "*") -> None:
        bucket = self._tokens.get(session_id)
        if not bucket:
            return
        bucket.pop(call_id, None)
        if not bucket:
            self._tokens.pop(session_id, None)

    def cancel(self, session_id: str, call_id: Optional[str] = None) -> list[str]:
        """Trip and return the affected ``call_id``s for ``session_id``.

        With ``call_id=None`` every token of the session is cancelled (used by
        ``user.interrupt``); otherwise only the named tool call is affected.
        """
        bucket = self._tokens.get(session_id)
        if not bucket:
            return []
        if call_id is None:
            affected = list(bucket)
            for token in bucket.values():
                token.cancel()
            bucket.clear()
            return affected
        token = bucket.pop(call_id, None)
        if token is None:
            return []
        token.cancel()
        if not bucket:
            self._tokens.pop(session_id, None)
        return [call_id]

    def is_cancelled(self, session_id: str, call_id: str = "*") -> bool:
        token = self._tokens.get(session_id, {}).get(call_id)
        return bool(token and token.cancelled)

    def __len__(self) -> int:
        return sum(len(bucket) for bucket in self._tokens.values())
