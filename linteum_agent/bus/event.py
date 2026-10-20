"""Event envelope, type registry and payload contracts (§5, §7, §8).

This is the single most important file of the whole framework: everything that
moves on the bus is an immutable, JSON-serialisable :class:`Event`.  Two
architectural levers live here and nowhere else:

* **P2 — payload must be serialisable.**  No callable / socket / handle ever
  reaches the WAL; :func:`assert_serializable` enforces this at the boundary.
* **P5 — big payloads are externalised** before the event is written, so that a
  crash can never leave an event pointing at a missing artifact (I12).

``seq`` and ``origin`` are *host-owned*: producers never set them, the bus does
(§5 field table).  :meth:`Event.child` inherits the causal chain (``trace_id``,
``session_id``, ...) so correlation is free.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from dataclasses import dataclass, replace
from typing import Any, Mapping

from .errors import InvalidEvent, NonSerializablePayload, UnknownEventType

# ---------------------------------------------------------------------------
# Constants (§5 / §8)
# ---------------------------------------------------------------------------

#: payload ≤ this stays inline; anything larger is externalised to an artifact.
INLINE_LIMIT = 32 * 1024
#: hard upper bound — above this the publish fails instead of silently truncating.
MAX_PAYLOAD_BYTES = 256 * 1024

#: Default origin stamped by the host for framework-owned producers (P8).
CORE_ORIGIN = "core"
DEFAULT_PRIORITY = 5

# ---------------------------------------------------------------------------
# Event type registry (§7)
# ---------------------------------------------------------------------------

#: Exact ``type`` values from the §7 tables (core, context/storage and plugin
#: lifecycle domains).  Prefix families (``hook.*``, ``plugin.<id>.*``,
#: ``channel.<name>.*``) are matched structurally by :func:`is_known_type`.
KNOWN_TYPES: frozenset = frozenset(
    {
        # §7.1 core domain
        "user.message",
        "user.interrupt",
        "llm.request",
        "llm.started",
        "llm.token.delta",
        "llm.reasoning.delta",
        "llm.completed",
        "llm.failed",
        "llm.params.dropped",
        "llm.warnings",
        "llm.context.transplanted",
        "tool.call.requested",
        "tool.call.started",
        "tool.output.delta",
        "tool.call.completed",
        "tool.call.failed",
        "tool.cancel.requested",
        "approval.required",
        "approval.granted",
        "approval.denied",
        "agent.plan.updated",
        "agent.handoff",
        "agent.paused",
        "agent.resumed",
        "agent.completed",
        "system.budget.exceeded",
        "system.provider.degraded",
        "system.provider.quarantined",
        # §7.2 context & storage domain
        "context.assembled",
        "context.compaction.requested",
        "context.compaction.applied",
        "retrieval.requested",
        "retrieval.completed",
        "memory.write.applied",
        "memory.deleted",
        "system.session.erased",
        # §7.3 plugin lifecycle (produced by PluginHost, hence core-origin)
        "system.plugin.loading",
        "system.plugin.loaded",
        "system.plugin.failed",
        "system.plugin.quiescing",
        "system.plugin.unloaded",
        "system.plugin.quarantined",
        "system.plugin.namespace_violation",
    }
)

#: Type families that are legitimate but open-ended.
_PREFIX_FAMILIES: tuple = ("hook.", "plugin.", "channel.")


def is_known_type(etype: str) -> bool:
    """True when ``etype`` hits the §7 enumeration (exact or prefix family)."""
    if etype in KNOWN_TYPES:
        return True
    return any(etype.startswith(p) for p in _PREFIX_FAMILIES)


def assert_known_type(etype: str) -> None:
    """Reject unknown types loudly: silent drops leave the front-end spinning."""
    if not isinstance(etype, str) or not etype:
        raise UnknownEventType("event type must be a non-empty string")
    if not is_known_type(etype):
        raise UnknownEventType(f"unknown event type: {etype!r}")


# ---------------------------------------------------------------------------
# uuid7 / clock helpers (§5)
# ---------------------------------------------------------------------------

def now() -> float:
    """Unix seconds with millisecond precision (the envelope's time base)."""
    return int(time.time() * 1000) / 1000.0


def uuid7() -> str:
    """Time-ordered UUIDv7 (RFC 9562): ``id`` is both sortable and a dedup key.

    Layout (MSB→LSB): 48-bit unix-ms | 4-bit version(7) | 12-bit rand_a |
    2-bit variant(10) | 62-bit rand_b.  Millisecond ordering is what makes the
    WAL append-only scan naturally chronological.
    """
    ms = int(time.time() * 1000) & 0xFFFFFFFFFFFF
    rnd = int.from_bytes(os.urandom(10), "big")
    rand_a = (rnd >> 68) & 0x0FFF
    rand_b = rnd & ((1 << 62) - 1)
    value = (ms << 80) | (0x7 << 76) | (rand_a << 64) | (0x2 << 62) | rand_b
    return str(uuid.UUID(int=value))


# ---------------------------------------------------------------------------
# Payload contracts (§8)
# ---------------------------------------------------------------------------

def dumps(payload: Mapping[str, Any]) -> str:
    """Canonical JSON encoding: UTF-8, no NaN/Infinity, no ASCII escaping."""
    return json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def assert_serializable(payload: Any) -> None:
    """Raise :class:`NonSerializablePayload` if the payload is not pure JSON.

    Non-string dict keys are also rejected so the WAL round-trip is lossless.
    """
    try:
        json.dumps(payload, allow_nan=False)
    except (TypeError, ValueError) as exc:  # callable/socket/handle/NaN/...
        raise NonSerializablePayload(str(exc)) from exc
    _assert_str_keys(payload)


def _assert_str_keys(value: Any) -> None:
    if isinstance(value, Mapping):
        for k, v in value.items():
            if not isinstance(k, str):
                raise NonSerializablePayload(f"payload keys must be str, got {type(k).__name__}")
            _assert_str_keys(v)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _assert_str_keys(item)


def payload_size(payload: Any) -> int:
    """Encoded byte-size of a payload (the value compared against the limits)."""
    return len(dumps(payload).encode("utf-8"))


def content_hash(text: str) -> str:
    """sha256 of an encoded payload — the content address for ``art:`` refs."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Event envelope (§5)
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class Event:
    """Immutable fact + routing/tracing shell.

    ``frozen`` + ``slots`` matches the design; immutability is what lets a
    router snapshot be shared without locking (I10 / RCU, §10).  ``payload`` is
    a plain dict and must be treated as read-only by convention.
    """

    # ── mandatory ──
    id: str
    type: str
    ts: float
    session_id: str
    payload: dict
    # ── tracing ──
    trace_id: str
    cause_id: str | None = None
    seq: int = 0
    # ── ownership (host-stamped) ──
    origin: str = CORE_ORIGIN
    # ── scheduling ──
    priority: int = DEFAULT_PRIORITY
    ttl_ms: int | None = None
    # ── evolution (§8) ──
    schema_version: int = 1

    # -- construction helpers ----------------------------------------------
    @classmethod
    def create(
        cls,
        etype: str,
        payload: dict,
        *,
        session_id: str,
        trace_id: str | None = None,
        origin: str = CORE_ORIGIN,
        priority: int = DEFAULT_PRIORITY,
        ttl_ms: int | None = None,
        schema_version: int = 1,
        cause_id: str | None = None,
    ) -> "Event":
        """Build a brand-new root event (the only place an id is minted).

        ``trace_id`` defaults to a fresh id, i.e. this event opens a new causal
        tree.  Use :meth:`child` to keep an existing tree.
        """
        ev_id = uuid7()
        return cls(
            id=ev_id,
            type=etype,
            ts=now(),
            session_id=session_id,
            payload=payload,
            trace_id=trace_id or ev_id,
            cause_id=cause_id,
            seq=0,
            origin=origin,
            priority=priority,
            ttl_ms=ttl_ms,
            schema_version=schema_version,
        )

    def child(self, etype: str, payload: dict, **kw: Any) -> "Event":
        """Derive a child event, inheriting trace/session/ownership metadata.

        The causal edge (``cause_id = self.id``) and the trace are carried over;
        ``seq`` is deliberately *not* trusted — the bus overwrites it on publish.
        """
        return replace(
            self,
            id=uuid7(),
            type=etype,
            payload=payload,
            cause_id=self.id,
            ts=now(),
            **kw,
        )

    # -- validation ----------------------------------------------------------
    def validate(self) -> None:
        """Enforce the §5 field constraints that the bus relies on."""
        if not isinstance(self.id, str) or not self.id:
            raise InvalidEvent("id must be a non-empty string")
        assert_known_type(self.type)
        if not isinstance(self.session_id, str) or not self.session_id:
            raise InvalidEvent("session_id must be a non-empty string (partition key)")
        if not isinstance(self.payload, dict):
            raise InvalidEvent("payload must be a dict")
        if not isinstance(self.ts, (int, float)):
            raise InvalidEvent("ts must be numeric")
        if not isinstance(self.trace_id, str) or not self.trace_id:
            raise InvalidEvent("trace_id must be a non-empty string")
        assert_serializable(self.payload)

    # -- (de)serialisation ---------------------------------------------------
    def to_dict(self) -> dict:
        """Full envelope as a plain dict (WAL row / transport representation)."""
        return {
            "id": self.id,
            "type": self.type,
            "ts": self.ts,
            "session_id": self.session_id,
            "payload": self.payload,
            "trace_id": self.trace_id,
            "cause_id": self.cause_id,
            "seq": self.seq,
            "origin": self.origin,
            "priority": self.priority,
            "ttl_ms": self.ttl_ms,
            "schema_version": self.schema_version,
        }

    def to_json(self) -> str:
        return dumps(self.to_dict())

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Event":
        return cls(
            id=data["id"],
            type=data["type"],
            ts=data["ts"],
            session_id=data["session_id"],
            payload=data.get("payload", {}),
            trace_id=data.get("trace_id", data["id"]),
            cause_id=data.get("cause_id"),
            seq=data.get("seq", 0),
            origin=data.get("origin", CORE_ORIGIN),
            priority=data.get("priority", DEFAULT_PRIORITY),
            ttl_ms=data.get("ttl_ms"),
            schema_version=data.get("schema_version", 1),
        )

    def with_payload(self, payload: dict, **kw: Any) -> "Event":
        """Replace the payload (used by artifact offloading / delta merging)."""
        return replace(self, payload=payload, **kw)


def make_event(etype: str, payload: dict, *, session_id: str, **kw: Any) -> Event:
    """Module-level convenience wrapper around :meth:`Event.create`."""
    return Event.create(etype, payload, session_id=session_id, **kw)
