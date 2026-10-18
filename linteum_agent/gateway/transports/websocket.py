"""WebSocket transport (§4.4 / §8).

The transport is a thin wrapper around one ``websockets`` connection: text
envelopes in, text envelopes out.  The ``websockets`` package is an **optional**
dependency (``linteum[web]``), so this module imports nothing from it; the class
only relies on the connection's ``recv``/``send``/``close`` methods.  The
server-side bootstrap (which does import ``websockets``) lives in
:mod:`linteum_agent.gateway.websocket_server`.

Default binding is loopback (``127.0.0.1``) and the protocol has no
authentication: the trust model is one local user on one machine (§6.8).
"""

from __future__ import annotations

from typing import Any, Optional

#: Names used by ``websockets`` for a normal peer close.  Matching by name keeps
#: this module importable when the optional dependency is absent.
_CLOSED_NAMES = frozenset({
    "ConnectionClosed", "ConnectionClosedOK", "ConnectionClosedError",
})


class WebSocketTransport:
    """One ``websockets`` connection exposed as a gateway transport."""

    def __init__(self, websocket: Any) -> None:
        self._websocket = websocket
        self._closed = False

    async def recv(self) -> Optional[str]:
        if self._closed:
            return None
        try:
            message = await self._websocket.recv()
        except Exception as exc:  # noqa: BLE001 - optional dependency type
            if type(exc).__name__ in _CLOSED_NAMES:
                return None
            raise
        if isinstance(message, bytes):
            try:
                return message.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ConnectionError("websocket frame is not valid UTF-8") from exc
        return message

    async def send(self, data: str | bytes) -> None:
        if self._closed:
            raise ConnectionError("websocket transport is closed")
        await self._websocket.send(data)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            await self._websocket.close()
        except Exception:  # noqa: BLE001 - closing a dead socket is fine
            pass


__all__ = ["WebSocketTransport"]
