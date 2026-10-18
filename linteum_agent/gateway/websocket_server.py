"""WebSocket server bootstrap (§4.4 / Phase 3).

Kept outside ``gateway/transports`` so the transport package stays free of the
gateway server (and of the optional ``websockets`` import).  This module lazily
imports ``websockets``; when it is missing the caller gets a clear install hint
for the ``linteum[web]`` extra.

Security posture (§6.8): the protocol has no authentication, so the default
bind is loopback.  Binding a non-loopback address requires ``allow_remote=True``
and is the operator's explicit decision (put a reverse proxy / socket
permissions in front of it).
"""

from __future__ import annotations

import asyncio
import ipaddress
from typing import Optional

from .transports.websocket import WebSocketTransport

DEFAULT_WS_HOST = "127.0.0.1"
DEFAULT_WS_PORT = 8765
DEFAULT_PING_INTERVAL = 20.0
DEFAULT_PING_TIMEOUT = 20.0
#: Envelope overhead allowance on top of the largest protocol payload.
WS_OVERHEAD = 64 * 1024


def _is_loopback(host: str) -> bool:
    if host in ("localhost", ""):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


async def serve_websocket(
    gateway_server,
    *,
    host: str = DEFAULT_WS_HOST,
    port: int = DEFAULT_WS_PORT,
    max_size: Optional[int] = None,
    ping_interval: float = DEFAULT_PING_INTERVAL,
    ping_timeout: float = DEFAULT_PING_TIMEOUT,
    stop: Optional[asyncio.Event] = None,
    ready: Optional[asyncio.Event] = None,
    allow_remote: bool = False,
) -> None:
    """Serve the gateway over WebSocket until ``stop`` is set.

    ``ping_interval`` / ``ping_timeout`` are WebSocket-level liveness probes;
    the application-level ``ping`` command is separate.
    """
    if not allow_remote and not _is_loopback(host):
        raise ValueError(
            f"refusing to bind {host!r}: the gateway has no authentication; "
            "use 127.0.0.1 or pass allow_remote=True behind your own proxy"
        )
    try:
        from websockets.asyncio.server import serve as ws_serve  # websockets>=13
    except ImportError:  # pragma: no cover - legacy 10–12 fallback
        try:
            from websockets.server import serve as ws_serve
        except ImportError as exc:
            raise RuntimeError(
                "websockets is required for --ws; install the optional extra: "
                "`uv add 'linteum[web]'` or `pip install 'websockets>=13'`"
            ) from exc

    if max_size is None:
        try:
            limits = gateway_server.settings.limits
            max_size = max(limits.max_command_bytes, limits.max_frame_bytes) + WS_OVERHEAD
        except Exception:  # noqa: BLE001 - a custom server may not expose settings
            max_size = 4 * 1024 * 1024
    stop = stop or asyncio.Event()
    ready = ready or asyncio.Event()

    async def handler(websocket, *_args) -> None:
        await gateway_server.serve(WebSocketTransport(websocket))

    async with ws_serve(
        handler,
        host,
        port,
        max_size=max_size,
        ping_interval=ping_interval,
        ping_timeout=ping_timeout,
    ):
        ready.set()
        await stop.wait()


__all__ = [
    "DEFAULT_PING_INTERVAL",
    "DEFAULT_PING_TIMEOUT",
    "DEFAULT_WS_HOST",
    "DEFAULT_WS_PORT",
    "serve_websocket",
]
