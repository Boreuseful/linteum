"""Namespace ownership and origin sealing (§6, P8, I7, I8).

The security trust root is deliberately tiny: a producer's ``origin`` is
*stamped by the host* (never self-declared by a plugin), and the bus validates
every publish and subscribe against the §6 ownership table.  The payoff is
structural — a buggy or malicious plugin **cannot** forge a tool result, a user
message or a context event, because no sequence of bytes it can produce will
pass :func:`assert_can_publish`.

Two invariants are enforced here and asserted in the test-suite:

* **I7** — a plugin publishing outside ``plugin.<id>.*`` always fails.
* **I8** — a plugin can never subscribe to ``tool.call.requested``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from .errors import CapabilityDenied, NamespaceViolation

if TYPE_CHECKING:  # avoid a runtime import cycle
    from .event import Event
    from .metrics import Metrics

#: Core domains owned by the framework; a non-core origin may not publish them.
CORE_DOMAINS: tuple = (
    "user",
    "llm",
    "tool",
    "approval",
    "agent",
    "hook",
    "context",
    "retrieval",
    "memory",
)

#: Prefixes used to classify an origin.
PLUGIN_PREFIX = "plugin:"
CHANNEL_PREFIX = "channel:"
CORE = "core"
SYSTEM_DOMAIN = "system"


# ---------------------------------------------------------------------------
# Origin predicates
# ---------------------------------------------------------------------------

def is_core(origin: str) -> bool:
    return origin == CORE


def is_plugin(origin: str) -> bool:
    return origin.startswith(PLUGIN_PREFIX)


def is_channel(origin: str) -> bool:
    return origin.startswith(CHANNEL_PREFIX)


def plugin_id(origin: str) -> Optional[str]:
    """Return the plugin id encoded in ``plugin:<id>`` (``None`` if not a plugin)."""
    if is_plugin(origin):
        return origin[len(PLUGIN_PREFIX):] or None
    return None


def stamp_origin(ev: "Event", origin: str) -> "Event":
    """Seal a producer identity onto an event (§6 / P8).

    This is the *only* sanctioned way to set ``origin``: plugin facades call it
    with ``plugin:<id>``, channels with ``channel:<name>`` and core code leaves
    the envelope's default.  The bus never trusts a client-supplied origin it
    has not been told to stamp.
    """
    from dataclasses import replace

    return replace(ev, origin=origin)


# ---------------------------------------------------------------------------
# Publish guard (I7)
# ---------------------------------------------------------------------------

def assert_can_publish(origin: str, etype: str, metrics: Optional["Metrics"] = None) -> None:
    """Raise :class:`NamespaceViolation` if ``origin`` may not publish ``etype``."""
    if is_core(origin):
        return

    # An authorized channel plugin may publish ``user.*`` (its own transport
    # surface) and its own ``channel.<name>.*`` namespace (§6 table).
    if is_channel(origin):
        name = origin[len(CHANNEL_PREFIX):]
        if etype.startswith(f"channel.{name}.") or etype.startswith("user."):
            return
        _violation(metrics, origin, etype)
        raise NamespaceViolation(f"{origin} 只能发布 channel.{name}.* 或 user.*")

    # Any other non-core origin may never publish a core-domain event.
    for domain in CORE_DOMAINS:
        if etype == domain or etype.startswith(domain + "."):
            _violation(metrics, origin, etype)
            raise NamespaceViolation(f"{origin} 无权发布核心事件 {etype}")

    if is_plugin(origin):
        pid = plugin_id(origin)
        if pid and etype.startswith(f"plugin.{pid}."):
            return
        _violation(metrics, origin, etype)
        raise NamespaceViolation(f"{origin} 只能发布 plugin.{pid}.*")

    _violation(metrics, origin, etype)
    raise NamespaceViolation(f"unknown origin {origin!r} cannot publish {etype}")


def _violation(metrics: Optional["Metrics"], origin: str, etype: str) -> None:
    if metrics is not None:
        metrics.namespace_violation(origin, etype)


# ---------------------------------------------------------------------------
# Subscribe guard (I8)
# ---------------------------------------------------------------------------

def assert_can_subscribe(origin: str, pattern: str, metrics: Optional["Metrics"] = None) -> None:
    """Capability check for subscriptions (§6 subscribe column, I8).

    Core may subscribe to anything.  A plugin is limited to its own namespace,
    the read-only tool outcome pair (``tool.call.completed`` / ``failed``) and
    the quota-controlled ``system.*`` observation channel — it can never grab
    ``tool.call.requested`` (which would let it impersonate the runtime).
    """
    if is_core(origin):
        return

    if is_plugin(origin):
        pid = plugin_id(origin)
        allowed = (
            pattern.startswith(f"plugin.{pid}.")
            or pattern in ("tool.call.completed", "tool.call.failed")
            or pattern == "system.*"
            or pattern.startswith("system.")
        )
        if allowed:
            return
        raise CapabilityDenied(f"{origin} 无权订阅 {pattern}")

    if is_channel(origin):
        name = origin[len(CHANNEL_PREFIX):]
        if pattern.startswith(f"channel.{name}.") or pattern == "user.*":
            return
        raise CapabilityDenied(f"{origin} 无权订阅 {pattern}")

    raise CapabilityDenied(f"unknown origin {origin!r} cannot subscribe {pattern}")


def can_subscribe(origin: str, pattern: str) -> bool:
    """Boolean form of :func:`assert_can_subscribe` (for capability probing)."""
    try:
        assert_can_subscribe(origin, pattern)
    except CapabilityDenied:
        return False
    return True
