"""Observability sidecar (§48): a dependency-free structured run log.

Records are written through the standard :mod:`logging` module to a size-rotated
``.log`` file (human-readable text by default, JSON-per-line optional).  The public
surface is intentionally tiny:

* :class:`~linteum_agent.runtime.observability.run_logger.RunLogger` — the run-log sidecar.
* :class:`~linteum_agent.runtime.observability.config.LogConfig` — its settings (+ ``AGENT_LOG_*``).
* :func:`~linteum_agent.runtime.observability.run_logger.redact` — field-level secret masking.
"""

from .config import LogConfig
from .run_logger import SENSITIVE_COMPONENTS, RunLogger, redact

__all__ = ["RunLogger", "LogConfig", "redact", "SENSITIVE_COMPONENTS"]
