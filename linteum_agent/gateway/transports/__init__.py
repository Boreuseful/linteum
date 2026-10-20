"""Gateway transports (§8).

A transport only frames bytes; it contains no session semantics.  ``inproc`` is
the embedded default; ``stdio`` (Phase 2) and ``websocket`` (Phase 3, optional
extra) implement the same :class:`~linteum_agent.gateway.transports.base.Transport`
protocol.
"""

from .base import Transport
from .inproc import InProcTransport, inproc_pair
from .stdio import StdioTransport
from .websocket import WebSocketTransport

__all__ = [
    "Transport",
    "InProcTransport",
    "StdioTransport",
    "WebSocketTransport",
    "inproc_pair",
]
