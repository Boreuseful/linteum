"""Event bus package (design doc §5–§15, S1 milestone).

Migrated from the ``new_projer`` experimental event-driven framework into
``linteum_agent`` so the linteum agent can run on an event-driven runtime.
Current scope is the **bus layer only** (plus its minimal ``scheduler`` /
structured-error dependencies); the agent reducer, LLM adapters and tool runtime
are not part of this package yet.

Public surface:

* :class:`~linteum_agent.bus.event.Event` — the immutable envelope.
* :class:`~linteum_agent.bus.bus.InMemoryEventBus` — the reference bus.
* :class:`~linteum_agent.bus.router.Router` — RCU subscription table.
* :class:`~linteum_agent.bus.fair_scheduler.OriginFairScheduler` — per-origin WFQ queues.
* :class:`~linteum_agent.bus.wal.InMemoryWal` / ``SqliteWal`` — write-ahead logs.
"""

from .artifact import (
    Artifact,
    ArtifactStore,
    InMemoryArtifactStore,
    NullArtifactStore,
    hydrate_event,
    hydrate_events,
)
from .backpressure import Backpressure, BackpressurePolicy, policy_for
from .bus import DedupSet, InMemoryEventBus
from .coalescer import DeltaCoalescer
from .errors import (
    ArtifactMissing,
    ArtifactUnavailable,
    BusClosed,
    BusError,
    BusOverflow,
    CapabilityDenied,
    ErrorCode,
    InvalidEvent,
    NamespaceViolation,
    NonSerializablePayload,
    PayloadTooLarge,
    UnknownEventType,
    UnknownRef,
)
from .event import (
    CORE_ORIGIN,
    DEFAULT_PRIORITY,
    INLINE_LIMIT,
    KNOWN_TYPES,
    MAX_PAYLOAD_BYTES,
    Event,
    assert_known_type,
    assert_serializable,
    is_known_type,
    make_event,
    now,
    payload_size,
    uuid7,
)
from .fair_scheduler import OriginFairScheduler
from .metrics import Metrics
from .metrics import metrics as default_metrics
from .namespace import (
    CORE,
    CORE_DOMAINS,
    assert_can_publish,
    assert_can_subscribe,
    can_subscribe,
    stamp_origin,
)
from .router import Router
from .wal import InMemoryWal, SqliteWal, WalStore

__all__ = [
    # event model
    "Event",
    "make_event",
    "uuid7",
    "now",
    "payload_size",
    "is_known_type",
    "assert_known_type",
    "assert_serializable",
    "known_types",
    "KNOWN_TYPES",
    "INLINE_LIMIT",
    "MAX_PAYLOAD_BYTES",
    "CORE_ORIGIN",
    "DEFAULT_PRIORITY",
    # namespaces
    "CORE",
    "CORE_DOMAINS",
    "assert_can_publish",
    "assert_can_subscribe",
    "can_subscribe",
    "stamp_origin",
    # bus
    "InMemoryEventBus",
    "DedupSet",
    # router / scheduling / backpressure
    "Router",
    "OriginFairScheduler",
    "Backpressure",
    "BackpressurePolicy",
    "policy_for",
    "DeltaCoalescer",
    # storage boundaries
    "Artifact",
    "ArtifactStore",
    "InMemoryArtifactStore",
    "NullArtifactStore",
    "hydrate_event",
    "hydrate_events",
    "WalStore",
    "InMemoryWal",
    "SqliteWal",
    # observability
    "Metrics",
    "default_metrics",
    # errors
    "ErrorCode",
    "BusError",
    "NamespaceViolation",
    "CapabilityDenied",
    "NonSerializablePayload",
    "PayloadTooLarge",
    "ArtifactUnavailable",
    "ArtifactMissing",
    "UnknownRef",
    "UnknownEventType",
    "InvalidEvent",
    "BusOverflow",
    "BusClosed",
]


def known_types() -> tuple:
    """Return the registered exact event types (sorted, for introspection)."""
    return tuple(sorted(KNOWN_TYPES))
