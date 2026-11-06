"""Background-task error vocabulary (async ReAct design v0.1 §16.2).

A background failure is data like every other failure (P4): the manager turns
:class:`BackgroundError` into ``background.start.failed`` / ``background.failed``
payloads carrying ``code``, ``error`` and a structured ``error_info`` whose
``source`` is :attr:`ErrorSource.BACKGROUND`.
"""

from __future__ import annotations

from typing import Optional

from ...bus.error_info import ErrorSource, error_info
from ...bus.errors import ErrorCode

E_BACKGROUND_DISABLED = ErrorCode.E_BACKGROUND_DISABLED.value
E_BACKGROUND_NOT_ALLOWED = ErrorCode.E_BACKGROUND_NOT_ALLOWED.value
E_BACKGROUND_UNKNOWN_TOOL = ErrorCode.E_BACKGROUND_UNKNOWN_TOOL.value
E_BACKGROUND_ARGS = ErrorCode.E_BACKGROUND_ARGS.value
E_BACKGROUND_LIMIT = ErrorCode.E_BACKGROUND_LIMIT.value
E_BACKGROUND_NOT_FOUND = ErrorCode.E_BACKGROUND_NOT_FOUND.value
E_BACKGROUND_NOT_RUNNING = ErrorCode.E_BACKGROUND_NOT_RUNNING.value
E_BACKGROUND_CANCELLED = ErrorCode.E_BACKGROUND_CANCELLED.value
E_BACKGROUND_STOPPED = ErrorCode.E_BACKGROUND_STOPPED.value
E_BACKGROUND_LOST = ErrorCode.E_BACKGROUND_LOST.value
E_BACKGROUND_TIMEOUT = ErrorCode.E_BACKGROUND_TIMEOUT.value
E_BACKGROUND_SPAWN = ErrorCode.E_BACKGROUND_SPAWN.value
E_BACKGROUND_APPROVAL = ErrorCode.E_BACKGROUND_APPROVAL.value

LOCATION_MANAGER = (
    "linteum_agent.runtime.background.manager.BackgroundTaskManager"
)


class BackgroundError(Exception):
    """One background-layer failure, convertible to an event payload."""

    code: str = "E_BACKGROUND_ERROR"
    retryable: bool = False

    def __init__(
        self,
        code: str,
        message: str,
        *,
        context: Optional[dict] = None,
        retryable: Optional[bool] = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.context = dict(context or {})
        if retryable is not None:
            self.retryable = retryable

    def as_error_info(self, *, location: Optional[str] = None) -> dict:
        return error_info(
            self.code,
            source=ErrorSource.BACKGROUND.value,
            location=location or LOCATION_MANAGER,
            message=self.message,
            retryable=self.retryable,
            enters_history=True,
            context=self.context,
        ).to_dict()

    def as_payload(self) -> dict:
        return {
            "code": self.code,
            "error": self.message,
            "error_info": self.as_error_info(),
        }


__all__ = [
    "BackgroundError",
    "E_BACKGROUND_APPROVAL",
    "E_BACKGROUND_ARGS",
    "E_BACKGROUND_CANCELLED",
    "E_BACKGROUND_DISABLED",
    "E_BACKGROUND_LIMIT",
    "E_BACKGROUND_LOST",
    "E_BACKGROUND_NOT_ALLOWED",
    "E_BACKGROUND_NOT_FOUND",
    "E_BACKGROUND_NOT_RUNNING",
    "E_BACKGROUND_SPAWN",
    "E_BACKGROUND_STOPPED",
    "E_BACKGROUND_TIMEOUT",
    "E_BACKGROUND_UNKNOWN_TOOL",
    "LOCATION_MANAGER",
]
