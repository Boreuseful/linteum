"""Gateway error codes and the ``error_info`` bridge (§5.5).

The gateway is a host layer, but its failures are still *data*: every command
error is reported with the same structured ``error_info`` shape the bus uses,
with ``source = "gateway"``.  Keeping this in one tiny module lets ``protocol``
stay free of bus imports for everything except this dependency-free helper.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

from ..bus.error_info import ErrorSource, error_info

#: Protocol-level errors (§5.5).
E_PROTOCOL = "E_PROTOCOL"
E_PROTOCOL_VERSION = "E_PROTOCOL_VERSION"
E_BAD_FRAME = "E_BAD_FRAME"
E_UNKNOWN_COMMAND = "E_UNKNOWN_COMMAND"
E_PAYLOAD_TOO_LARGE = "E_PAYLOAD_TOO_LARGE"

#: Session / lifecycle errors.
E_SESSION_NOT_FOUND = "E_SESSION_NOT_FOUND"
E_SESSION_LOCKED = "E_SESSION_LOCKED"
E_SESSION_WORKSPACE = "E_SESSION_WORKSPACE"

#: Command semantics.
E_BUSY = "E_BUSY"
E_SLOW_CONSUMER = "E_SLOW_CONSUMER"
E_APPROVAL_GONE = "E_APPROVAL_GONE"
E_REQUEST_TIMEOUT = "E_REQUEST_TIMEOUT"
E_UNKNOWN_REF = "E_UNKNOWN_REF"
E_MODE_BUSY = "E_MODE_BUSY"

#: Internal fallback.
E_INTERNAL = "E_INTERNAL"

#: Stable location strings embedded in ``error_info``.
LOCATION_PROTOCOL = "linteum_agent.gateway.protocol"
LOCATION_SERVER = "linteum_agent.gateway.server.GatewayServer"
LOCATION_SESSION = "linteum_agent.gateway.session.SessionGateway"


class GatewayError(Exception):
    """A command/frame failure carrying a structured ``error_info`` payload."""

    def __init__(
        self,
        code: str,
        message: str = "",
        *,
        retryable: bool = False,
        context: Optional[Mapping[str, Any]] = None,
        location: Optional[str] = None,
        exc: Optional[BaseException] = None,
    ) -> None:
        super().__init__(message or code)
        self.code = code
        self.message = message or code
        self.retryable = retryable
        self.context = dict(context or {})
        self.location = location
        self.info = error_info(
            code,
            source=ErrorSource.GATEWAY.value,
            location=location or LOCATION_PROTOCOL,
            message=self.message,
            exc=exc,
            retryable=retryable,
            enters_history=False,
            context=self.context,
        )

    def to_dict(self) -> dict:
        return self.info.to_dict()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"GatewayError({self.code!r}, {self.message!r})"


__all__ = [
    "E_APPROVAL_GONE",
    "E_BAD_FRAME",
    "E_BUSY",
    "E_INTERNAL",
    "E_PAYLOAD_TOO_LARGE",
    "E_PROTOCOL",
    "E_PROTOCOL_VERSION",
    "E_REQUEST_TIMEOUT",
    "E_SESSION_LOCKED",
    "E_SESSION_NOT_FOUND",
    "E_SESSION_WORKSPACE",
    "E_SLOW_CONSUMER",
    "E_UNKNOWN_COMMAND",
    "E_UNKNOWN_REF",
    "GatewayError",
    "LOCATION_PROTOCOL",
    "LOCATION_SERVER",
    "LOCATION_SESSION",
]
