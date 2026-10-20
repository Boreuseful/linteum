"""In-process transport (§4.4 Embedded).

Embedded CLI/TUI still go through the gateway: a pair of ``asyncio.Queue``
objects stands in for the socket.  The one queue hop costs microseconds next to
an LLM call, and in exchange the front-end exercises the same protocol,
projection and backpressure path as the Web client (§3.5).

The queues are intentionally unbounded: the *client connection* (server side)
owns the bounded outbound queue, so a slow consumer is dropped/gapped by the
gateway rather than silently blocking the bus.
"""

from __future__ import annotations

import asyncio
from typing import Optional


class InProcTransport:
    """One end of an in-memory, full-duplex JSON message channel."""

    def __init__(self, inbox: asyncio.Queue, outbox: asyncio.Queue) -> None:
        self._inbox = inbox
        self._outbox = outbox
        self._closed = False

    async def recv(self) -> Optional[str | bytes]:
        return await self._inbox.get()

    async def send(self, data: str | bytes) -> None:
        if self._closed:
            raise ConnectionError("transport is closed")
        await self._outbox.put(data)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        # A sentinel unblocks the peer's ``recv`` even when no message is queued.
        await self._outbox.put(None)


def inproc_pair() -> tuple[InProcTransport, InProcTransport]:
    """Return ``(client_end, server_end)`` connected back to back."""
    client_to_server: asyncio.Queue = asyncio.Queue()
    server_to_client: asyncio.Queue = asyncio.Queue()
    client = InProcTransport(inbox=server_to_client, outbox=client_to_server)
    server = InProcTransport(inbox=client_to_server, outbox=server_to_client)
    return client, server


__all__ = ["InProcTransport", "inproc_pair"]
