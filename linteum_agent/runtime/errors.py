"""Shared structured error detail (``error_info``).

The single source of truth lives in :mod:`linteum_agent.bus.error_info`; this
module re-exports it under the runtime package so the migrated agent / state /
tools modules keep their original ``from .errors`` and ``from ..errors`` imports
without duplicating the implementation.
"""

from ..bus.error_info import (
    DEFAULT_DETAIL_LIMIT,
    ErrorInfo,
    ErrorSource,
    error_info,
    error_payload,
    explain,
    frame_location,
    get_error_info,
)

__all__ = [
    "DEFAULT_DETAIL_LIMIT",
    "ErrorInfo",
    "ErrorSource",
    "error_info",
    "error_payload",
    "explain",
    "frame_location",
    "get_error_info",
]
