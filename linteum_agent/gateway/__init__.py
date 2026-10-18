"""Gateway: the stable command/frame boundary between front-ends and the bus (§0).

Front-ends never publish or subscribe to the bus directly.  They send
whitelisted **commands** and receive projected **frames**:

* :mod:`~linteum_agent.gateway.protocol` — v1 envelopes, validation, errors;
* :mod:`~linteum_agent.gateway.projector` — pure event → view projection;
* :mod:`~linteum_agent.gateway.session` — per-session pump, ordering, fan-out,
  idempotency, busy policy, approvals and history;
* :mod:`~linteum_agent.gateway.server` — connections, limits, audit and the
  session catalogue;
* :mod:`~linteum_agent.gateway.client` — transport-independent client SDK;
* :mod:`~linteum_agent.gateway.transports` — inproc / stdio / WebSocket framing;
* :mod:`~linteum_agent.gateway.leases` — cross-process single-writer leases;
* :mod:`~linteum_agent.gateway.log` — the ``linteum.gateway`` logger hierarchy.

Dependency direction (§4.3): ``frontend → client → transport → gateway →
runtime → bus``.  ``bus`` never imports ``gateway``; transports import only
``protocol``; the client imports no runtime code.  The runtime-facing objects
(server/session/projector) are **lazily** exported here so importing the client
SDK alone does not pull in the runtime.
"""

from __future__ import annotations

import importlib
from typing import Any

from .client import AgentClient, ClientError, ConnectionClosed, open_client
from .errors import GatewayError
from .leases import FileLeaseManager, Lease, LeaseError, LeaseHeld
from .protocol import (
    PROTOCOL_VERSION,
    Command,
    CommandResult,
    Frame,
    decode,
    encode,
)
from .transports import (
    InProcTransport,
    StdioTransport,
    Transport,
    WebSocketTransport,
    inproc_pair,
)

#: Runtime-facing exports are resolved on first access (see ``__getattr__``).
_LAZY: dict[str, tuple[str, str]] = {
    "ClientConnection": (".server", "ClientConnection"),
    "GatewayServer": (".server", "GatewayServer"),
    "SessionGateway": (".session", "SessionGateway"),
    "ProjectedFrame": (".projector", "ProjectedFrame"),
    "ProjectorState": (".projector", "ProjectorState"),
    "project": (".projector", "project"),
    "snapshot_payload": (".projector", "snapshot_payload"),
}

__all__ = [
    "AgentClient",
    "ClientConnection",
    "ClientError",
    "Command",
    "CommandResult",
    "ConnectionClosed",
    "FileLeaseManager",
    "Frame",
    "GatewayError",
    "GatewayServer",
    "InProcTransport",
    "Lease",
    "LeaseError",
    "LeaseHeld",
    "PROTOCOL_VERSION",
    "ProjectedFrame",
    "ProjectorState",
    "SessionGateway",
    "StdioTransport",
    "Transport",
    "WebSocketTransport",
    "decode",
    "encode",
    "inproc_pair",
    "open_client",
    "project",
    "snapshot_payload",
]


def __getattr__(name: str) -> Any:
    try:
        module_name, attribute = _LAZY[name]
    except KeyError as exc:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from exc
    module = importlib.import_module(module_name, __name__)
    value = getattr(module, attribute)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_LAZY))
