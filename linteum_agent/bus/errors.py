"""Bus-layer errors and the canonical error-code table (Appendix A).

The framework's fourth design principle is **"一切错误都是数据"** — errors never
escape onto the bus, they are converted into events.  This module therefore only
defines the *boundary* exceptions that the bus itself raises when a producer
violates a contract (namespace, serialisation, payload size).  Handler failures
are *not* exceptions here: they are turned into dead-letter records (see
`bus.InMemoryEventBus._safe`).

Only the subset of Appendix A that the event bus can raise is enumerated; the
full table lives in the design document and is extended by later stages (tools,
adapters, ...).
"""

from __future__ import annotations

from enum import Enum

from .error_info import ErrorSource, error_info, frame_location


class ErrorCode(str, Enum):
    """Canonical error codes (Appendix A), used verbatim in event payloads."""

    # -- bus / namespace (this stage) --
    E_NS_VIOLATION = "E_NS_VIOLATION"
    E_CAP_DENIED = "E_CAP_DENIED"
    E_BUS_OVERFLOW = "E_BUS_OVERFLOW"
    E_BAD_ARGS = "E_BAD_ARGS"
    E_ARTIFACT_UNAVAILABLE = "E_ARTIFACT_UNAVAILABLE"
    E_ARTIFACT_MISSING = "E_ARTIFACT_MISSING"
    E_REPLAY_DIVERGENCE = "E_REPLAY_DIVERGENCE"
    # -- referenced by the bus but produced downstream --
    E_UNKNOWN_TOOL = "E_UNKNOWN_TOOL"
    E_TIMEOUT = "E_TIMEOUT"
    E_CANCELLED = "E_CANCELLED"
    # -- tools & plugins (produced by the tools / plugin layers, Appendix A) --
    E_PLUGIN_UNLOADING = "E_PLUGIN_UNLOADING"
    E_PLUGIN_UNAVAILABLE = "E_PLUGIN_UNAVAILABLE"
    E_PLUGIN_ERROR = "E_PLUGIN_ERROR"
    E_DENIED = "E_DENIED"
    E_TOOL_ERROR = "E_TOOL_ERROR"
    E_SANDBOX = "E_SANDBOX"
    E_BUDGET = "E_BUDGET"
    E_SAFETY = "E_SAFETY"
    # -- skills (produced by the runtime/skills tool layer) --
    E_SKILL_NOT_FOUND = "E_SKILL_NOT_FOUND"
    E_SKILL_RESOURCE_NOT_FOUND = "E_SKILL_RESOURCE_NOT_FOUND"
    E_SKILL_PATH = "E_SKILL_PATH"
    E_SKILL_LIMIT = "E_SKILL_LIMIT"
    E_SKILL_INVALID = "E_SKILL_INVALID"


class BusError(Exception):
    """Base class for every bus boundary violation.

    Attributes mirror Appendix A so that a failure can be logged or turned into a
    ``system.*`` event without losing the retry / history semantics.  ``source``
    pins the failure to the bus layer and :meth:`as_payload` adds a structured
    ``error_info`` (location + original message) on top of ``code`` / ``error``.
    """

    code: str = "E_BUS_ERROR"
    source: str = ErrorSource.BUS.value
    retryable: bool = False
    enters_history: bool = False

    def __init__(self, message: str = "", *, location: str | None = None, **context: object) -> None:
        super().__init__(message or self.__class__.__name__)
        self.context = context
        # capture the raise site here (raise happens right after construction)
        self.location = location if location is not None else frame_location(1)

    def error_info(self):
        """Structured detail (source=bus) for this boundary violation."""
        return error_info(
            self.code,
            source=self.source,
            location=self.location,
            message=str(self),
            retryable=self.retryable,
            enters_history=self.enters_history,
            context=self.context,
        )

    def as_payload(self) -> dict:
        """Render this error as a JSON-serialisable payload fragment."""
        return {"code": self.code, "error": str(self), **self.context,
                "error_info": self.error_info().to_dict()}


class NonSerializablePayload(BusError, TypeError):
    """P2: payload must be pure JSON. A callable / socket / handle raises here."""

    code = ErrorCode.E_BAD_ARGS.value


class PayloadTooLarge(BusError):
    """Payload exceeds ``MAX_PAYLOAD_BYTES`` and no artifact store is available."""

    code = ErrorCode.E_ARTIFACT_UNAVAILABLE.value


class ArtifactUnavailable(BusError):
    """Blob storage is unavailable; the payload cannot be externalised (§32.1)."""

    code = ErrorCode.E_ARTIFACT_UNAVAILABLE.value
    retryable = True
    enters_history = True


class ArtifactMissing(BusError):
    """An ``art:`` reference could not be resolved (violates I12)."""

    code = ErrorCode.E_ARTIFACT_MISSING.value
    enters_history = True


class UnknownRef(BusError):
    """A reference prefix is not ``art:`` / ``evt:`` / ``mem:`` (§32.2)."""

    code = ErrorCode.E_ARTIFACT_MISSING.value


class UnknownEventType(BusError):
    """``ev.type`` does not hit the §7 enumeration; silently dropping would hang the UI."""

    code = ErrorCode.E_BAD_ARGS.value


class InvalidEvent(BusError):
    """A mandatory envelope field is missing or malformed (§5)."""

    code = ErrorCode.E_BAD_ARGS.value


class NamespaceViolation(BusError):
    """P8 / I7: a producer tried to publish outside its namespace (security event)."""

    code = ErrorCode.E_NS_VIOLATION.value


class CapabilityDenied(BusError):
    """I8: a plugin tried to subscribe to a pattern outside its capability set."""

    code = ErrorCode.E_CAP_DENIED.value


class BusOverflow(BusError):
    """Queue full and the event class is not shed-able (§13.1)."""

    code = ErrorCode.E_BUS_OVERFLOW.value
    retryable = True


class BusClosed(BusError):
    """A publish/subscribe happened after ``close()``."""

    code = ErrorCode.E_BUS_OVERFLOW.value
