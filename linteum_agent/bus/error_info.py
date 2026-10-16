"""Structured error detail carried alongside failure-event error codes (§26/§31).

A failure event has always carried a ``code`` — but several codes are *shared*
across layers: ``E_TIMEOUT``, ``E_BAD_ARGS`` and ``E_CANCELLED`` can come from
either the tool runtime or the LLM adapter, so the code alone is ambiguous
("是 tools 报错还是 llm 适配器报错？").  :class:`ErrorInfo` fixes that by adding,
**on top of** the code (never replacing it):

* ``source``   — which layer produced it (``bus`` / ``llm`` / ``tool`` / …);
* ``location`` — ``module.symbol:line`` where it was raised;
* ``message``  — short human summary (same text as ``payload["error"]``);
* ``detail``   — the **original raw error** text (provider body, tool exception…);
* ``exc_type`` — the original exception class name;
* ``retryable`` / ``enters_history`` — Appendix-A semantics;
* ``context``  — extra structured bits (``tool``, ``provider``, ``model`` …).

Every failure payload therefore keeps ``code`` / ``error`` (backward compatible)
and gains an ``error_info`` object::

    {"code": "E_TIMEOUT", "error": "...",
     "error_info": {"code": "E_TIMEOUT", "source": "tool",
                    "location": "agent.tools.runtime.ToolRuntime._execute",
                    "message": "timeout after 30s", "detail": "TimeoutError: ...",
                    "exc_type": "TimeoutError", "retryable": true,
                    "enters_history": true, "context": {"tool": "web_search"}}}

This module is dependency-free (imports nothing from the rest of the package), so
both :mod:`linteum_agent.bus.errors` and any future tool/LLM error module can
build on it without a cycle.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Optional

DEFAULT_DETAIL_LIMIT = 1000


class ErrorSource(str, Enum):
    """Which layer of the framework produced a failure (the missing dimension)."""

    BUS = "bus"
    LLM = "llm"
    TOOL = "tool"
    SAFETY = "safety"
    SANDBOX = "sandbox"
    AGENT = "agent"
    CONTEXT = "context"
    STORAGE = "storage"
    PLUGIN = "plugin"
    HOST = "host"
    UNKNOWN = "unknown"


def frame_location(skip: int = 0) -> str:
    """Return ``module.symbol:line`` for a frame above the caller (best effort).

    ``skip=0`` is the caller of :func:`frame_location`; ``skip=1`` its caller,
    and so on.  Never raises — a missing frame degrades to an empty string.
    """
    try:
        frame = sys._getframe(1 + skip)
    except ValueError:  # pragma: no cover - only at the bottom of the stack
        return ""
    code = frame.f_code
    return f"{frame.f_globals.get('__name__', '?')}.{code.co_name}:{frame.f_lineno}"


def _truncate(text: str, limit: Optional[int]) -> str:
    if not limit or len(text) <= limit:
        return text
    return text[:limit] + "…"


@dataclass(frozen=True)
class ErrorInfo:
    """Structured, JSON-serialisable detail for one failure (extends a ``code``)."""

    code: str
    source: str = ErrorSource.UNKNOWN.value
    location: str = ""
    message: str = ""
    exc_type: Optional[str] = None
    detail: str = ""
    retryable: bool = False
    enters_history: bool = True
    context: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "source": self.source,
            "location": self.location,
            "message": self.message,
            "exc_type": self.exc_type,
            "detail": self.detail,
            "retryable": self.retryable,
            "enters_history": self.enters_history,
            "context": dict(self.context),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ErrorInfo":
        return cls(
            code=str(data.get("code", "")),
            source=str(data.get("source", ErrorSource.UNKNOWN.value)),
            location=str(data.get("location", "")),
            message=str(data.get("message", "")),
            exc_type=data.get("exc_type"),
            detail=str(data.get("detail", "")),
            retryable=bool(data.get("retryable", False)),
            enters_history=bool(data.get("enters_history", True)),
            context=dict(data.get("context") or {}),
        )


def error_info(
    code: str,
    *,
    source: str = ErrorSource.UNKNOWN.value,
    location: Optional[str] = None,
    message: str = "",
    exc: Optional[BaseException] = None,
    exc_type: Optional[str] = None,
    detail: Optional[str] = None,
    retryable: bool = False,
    enters_history: bool = True,
    context: Optional[Mapping[str, Any]] = None,
    detail_limit: Optional[int] = DEFAULT_DETAIL_LIMIT,
) -> ErrorInfo:
    """Build an :class:`ErrorInfo`, deriving ``exc_type``/``detail`` from ``exc``.

    ``detail`` (the original raw error) is taken from ``exc`` unless supplied —
    adapters pass their richer provider text here.  ``location`` is auto-captured
    from the call site when not given.
    """
    if exc is not None:
        if exc_type is None:
            exc_type = type(exc).__name__
        if detail is None:
            text = str(exc)
            detail = f"{type(exc).__name__}: {text}" if text else type(exc).__name__
    return ErrorInfo(
        code=str(code),
        source=source,
        location=location if location is not None else frame_location(1),
        message=message or (detail or str(code)),
        exc_type=exc_type,
        detail=_truncate(detail or "", detail_limit),
        retryable=retryable,
        enters_history=enters_history,
        context=dict(context or {}),
    )


def error_payload(
    *,
    code: str,
    source: str,
    message: str = "",
    location: Optional[str] = None,
    exc: Optional[BaseException] = None,
    detail: Optional[str] = None,
    retryable: bool = False,
    enters_history: bool = True,
    context: Optional[Mapping[str, Any]] = None,
    detail_limit: Optional[int] = DEFAULT_DETAIL_LIMIT,
    **extra: Any,
) -> dict:
    """A full failure payload: ``code`` + ``error`` + ``error_info`` (+ extras).

    Keeps the historical ``code``/``error`` keys so existing subscribers keep
    working, and adds the structured ``error_info`` object for everyone else.
    """
    info = error_info(
        code,
        source=source,
        location=location,
        message=message,
        exc=exc,
        detail=detail,
        retryable=retryable,
        enters_history=enters_history,
        context=context,
        detail_limit=detail_limit,
    )
    return {"code": info.code, "error": info.message, "error_info": info.to_dict(), **extra}


def get_error_info(payload: Any) -> Optional[ErrorInfo]:
    """Read the :class:`ErrorInfo` back out of a failure payload (or ``None``).

    Accepts a plain payload dict *or* anything exposing ``.payload`` (an Event).
    """
    if payload is None:
        return None
    data = getattr(payload, "payload", payload)
    if not isinstance(data, Mapping):
        return None
    raw = data.get("error_info")
    if raw is None:
        return None
    return ErrorInfo.from_dict(raw)


def explain(payload: Any) -> str:
    """One-line human summary for logs / error boards: ``type source/code @ loc``.

    Falls back gracefully for legacy payloads that only carry ``code``/``error``.
    """
    etype = getattr(payload, "type", "") or ""
    data = getattr(payload, "payload", payload)
    if not isinstance(data, Mapping):
        return f"{etype} {data!r}".strip()
    info = get_error_info(data)
    prefix = f"{etype} " if etype else ""
    if info is None:
        return f"{prefix}code={data.get('code')} error={data.get('error')}".strip()
    detail = info.message or info.detail
    return f"{prefix}{info.source}/{info.code} @ {info.location or '?'}: {detail}".strip()

