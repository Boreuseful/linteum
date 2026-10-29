"""Structured errors for the mode layer (§15.2).

``runtime.modes`` is a leaf: these errors are plain Python exceptions with a
stable ``code`` string.  ``AgentHost`` wraps them in ``ConfigError`` at the
host boundary, so this module never imports ``config`` or ``bus``.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional


class ModeError(Exception):
    """Base class for every mode failure.

    ``code`` matches the ``E_MODE_*`` names in the design (and the
    ``ErrorCode`` enum added by the host integration).  ``key``/``hint``/
    ``context`` mirror :class:`linteum_agent.config.schema.ConfigError` so the
    host can preserve the diagnostic detail when wrapping.
    """

    code: str = "E_MODE_ERROR"
    retryable: bool = False

    def __init__(
        self,
        message: str = "",
        *,
        key: Optional[str] = None,
        hint: str = "",
        context: Optional[Mapping[str, Any]] = None,
    ) -> None:
        super().__init__(message or self.__class__.__name__)
        self.key = key
        self.hint = hint
        self.context = dict(context or {})

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "error": str(self),
            "key": self.key,
            "hint": self.hint,
            "context": dict(self.context),
        }


class ModeNotFound(ModeError):
    code = "E_MODE_NOT_FOUND"


class ModeInvalid(ModeError):
    code = "E_MODE_INVALID"


class ModeExtends(ModeError):
    code = "E_MODE_EXTENDS"


class ModeTemplate(ModeError):
    code = "E_MODE_TEMPLATE"


class ModeLimit(ModeError):
    code = "E_MODE_LIMIT"


class ModeReserved(ModeError):
    code = "E_MODE_RESERVED"


class ModeBusy(ModeError):
    code = "E_MODE_BUSY"
    retryable = True


class ModeImmutable(ModeError):
    code = "E_MODE_IMMUTABLE"


class ModeSnapshotMissing(ModeError):
    code = "E_MODE_SNAPSHOT_MISSING"


class ModeSnapshotInvalid(ModeError):
    code = "E_MODE_SNAPSHOT_INVALID"


__all__ = [
    "ModeBusy",
    "ModeError",
    "ModeExtends",
    "ModeImmutable",
    "ModeInvalid",
    "ModeLimit",
    "ModeNotFound",
    "ModeReserved",
    "ModeSnapshotInvalid",
    "ModeSnapshotMissing",
    "ModeTemplate",
]
