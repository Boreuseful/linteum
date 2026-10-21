"""Structured ``E_SUBAGENT_*`` failures (subagent design §10.1, §16).

Every orchestrator failure becomes data: the caller catches
:class:`SubagentError` and publishes a ``subagent.failed`` payload built from
:meth:`SubagentError.as_error_info`.  Codes are stable strings (the codebase
style), ``source`` is always ``ErrorSource.SUBAGENT`` and the full context
(parent/child/call/phase) travels in ``error_info.context``.
"""

from __future__ import annotations

from typing import Any, Mapping

from ...bus.error_info import ErrorSource, error_info

E_SUBAGENT_DISABLED = "E_SUBAGENT_DISABLED"
E_SUBAGENT_INVALID = "E_SUBAGENT_INVALID"
E_SUBAGENT_POLICY = "E_SUBAGENT_POLICY"
E_SUBAGENT_DEPTH = "E_SUBAGENT_DEPTH"
E_SUBAGENT_BUSY = "E_SUBAGENT_BUSY"
E_SUBAGENT_TIMEOUT = "E_SUBAGENT_TIMEOUT"
E_SUBAGENT_CANCELLED = "E_SUBAGENT_CANCELLED"
E_SUBAGENT_BUDGET = "E_SUBAGENT_BUDGET"
E_SUBAGENT_FAILED = "E_SUBAGENT_FAILED"
E_SUBAGENT_SPAWN = "E_SUBAGENT_SPAWN"
E_SUBAGENT_UNSUPPORTED = "E_SUBAGENT_UNSUPPORTED"
E_SESSION_SUBAGENT = "E_SESSION_SUBAGENT"

#: Codes a model may reasonably retry with a corrected brief.
RETRYABLE_CODES = frozenset({
    E_SUBAGENT_BUSY, E_SUBAGENT_TIMEOUT, E_SUBAGENT_CANCELLED,
    E_SUBAGENT_FAILED, E_SUBAGENT_SPAWN,
})

#: This code is a host-side guard, not a fact about the parent transcript.
NON_HISTORY_CODES = frozenset({E_SESSION_SUBAGENT})


class SubagentError(Exception):
    """One structured subagent failure (message + code + context)."""

    def __init__(
        self,
        code: str,
        message: str = "",
        *,
        retryable: bool | None = None,
        context: Mapping[str, Any] | None = None,
        location: str | None = None,
    ) -> None:
        super().__init__(message or code)
        self.code = code
        self.message = message or code
        self.retryable = code in RETRYABLE_CODES if retryable is None else retryable
        self.context = dict(context or {})
        self.location = location

    def as_error_info(self) -> dict:
        """The JSON ``error_info`` object carried by ``subagent.failed``."""
        return error_info(
            self.code,
            source=ErrorSource.SUBAGENT.value,
            location=self.location,
            message=self.message,
            retryable=self.retryable,
            enters_history=self.code not in NON_HISTORY_CODES,
            context=self.context,
        ).to_dict()

    def payload(self, **extra: Any) -> dict:
        """A complete failure payload (``ok``/``code``/``error``/``error_info``)."""
        return {
            "ok": False,
            "code": self.code,
            "error": self.message,
            "error_info": self.as_error_info(),
            **extra,
        }


__all__ = [
    "E_SESSION_SUBAGENT",
    "E_SUBAGENT_BUDGET",
    "E_SUBAGENT_BUSY",
    "E_SUBAGENT_CANCELLED",
    "E_SUBAGENT_DEPTH",
    "E_SUBAGENT_DISABLED",
    "E_SUBAGENT_FAILED",
    "E_SUBAGENT_INVALID",
    "E_SUBAGENT_POLICY",
    "E_SUBAGENT_SPAWN",
    "E_SUBAGENT_TIMEOUT",
    "E_SUBAGENT_UNSUPPORTED",
    "NON_HISTORY_CODES",
    "RETRYABLE_CODES",
    "SubagentError",
]
