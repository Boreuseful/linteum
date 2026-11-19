"""Runtime apply scope for configuration keys (web-config design §7.2).

``config.meta.apply_scope`` classifies the *intended* lifetime of a value.
This module answers the narrower question the UI must show: when will a
running gateway actually read the new value?  It is a pure lookup table with
no I/O and no imports from ``runtime``.
"""

from __future__ import annotations

from typing import Iterable

#: The running process re-reads the value on every use.
LIVE = "live"
#: A new connection or session view picks the value up.
NEXT_CONNECTION = "next_connection"
#: Only sessions created after the write use the value.
NEW_SESSION = "new_session"
#: The gateway must restart.
RESTART = "restart"
#: The server does not consume the value; the client owns it.
CLIENT = "client"
#: The current runtime has no consumer for the value.
NOOP = "noop"

_RESTART_PREFIXES: tuple[str, ...] = (
    "storage.",
    "gateway.websocket.",
    "gateway.plugins.",
    "gateway.config.",
    "agent.plugins.",
)
_RESTART_KEYS = frozenset({
    "gateway.transport",
    # The bus (and its coalescer) is constructed when AgentHost starts.
    "gateway.delta.coalesce_window_ms",
})

#: RunLogger.apply_config() swaps level/handlers in place (Phase 4).
_LIVE_PREFIXES: tuple[str, ...] = ("logging.",)

_NEXT_CONNECTION_PREFIXES: tuple[str, ...] = (
    "gateway.limits.",
    "gateway.history.",
)
_NEXT_CONNECTION_KEYS = frozenset({
    # SessionGateway captures this when the session view is created.
    "agent.session.input_policy",
})

_NOOP_PREFIXES: tuple[str, ...] = ("gateway.backpressure.",)
_NOOP_KEYS = frozenset({"agent.session.idle_ttl_ms"})

_CLIENT_PREFIXES: tuple[str, ...] = ("ui.",)


def runtime_scope(key: str) -> str:
    """Return the real effect of changing ``key`` in a running gateway."""
    if key in _RESTART_KEYS or key.startswith(_RESTART_PREFIXES):
        return RESTART
    if key.startswith(_LIVE_PREFIXES):
        return LIVE
    if key in _NEXT_CONNECTION_KEYS or key.startswith(_NEXT_CONNECTION_PREFIXES):
        return NEXT_CONNECTION
    if key in _NOOP_KEYS or key.startswith(_NOOP_PREFIXES):
        return NOOP
    if key.startswith(_CLIENT_PREFIXES):
        return CLIENT
    return NEW_SESSION


def runtime_scopes(keys: Iterable[str]) -> dict[str, str]:
    """Return ``{key: runtime_scope(key)}`` for one response."""
    return {key: runtime_scope(key) for key in keys}


__all__ = [
    "CLIENT",
    "LIVE",
    "NEW_SESSION",
    "NEXT_CONNECTION",
    "NOOP",
    "RESTART",
    "runtime_scope",
    "runtime_scopes",
]
