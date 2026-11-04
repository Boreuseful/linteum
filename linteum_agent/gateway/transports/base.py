"""Transport boundary: framed JSON in, framed JSON out (§4.2 / §8).

A transport owns *only* connection mechanics.  It knows nothing about sessions,
commands, frames or the bus — the gateway server reads envelopes from
``recv()`` and writes them through ``send()``.  The same :class:`Transport`
protocol therefore backs in-process queues, newline-delimited stdio and
WebSocket frames without changing any session semantics.

``recv()`` returns ``None`` when the peer closed the stream; callers must treat
that as EOF, not as an empty message.
"""

from __future__ import annotations

from typing import Optional, Protocol, runtime_checkable


@runtime_checkable
class Transport(Protocol):
    """One bidirectional, message-framed connection."""

    async def recv(self) -> Optional[str | bytes]: ...

    async def send(self, data: str | bytes) -> None: ...

    async def close(self) -> None: ...


__all__ = ["Transport"]
