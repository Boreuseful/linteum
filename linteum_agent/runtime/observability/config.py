"""Configuration for the run logger (§48).

Everything is overridable in code via :class:`LogConfig`, and every field has an
environment fallback (``AGENT_LOG_*``) so a host can turn logging on without
touching source.  Defaults are deliberately conservative: metadata only, a single
``.log`` file rotated by size, written through the standard :mod:`logging` module.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

#: Environment variable names (one per field).
ENV_DIR = "AGENT_LOG_DIR"
ENV_FILE = "AGENT_LOG_FILE"
ENV_LEVEL = "AGENT_LOG_LEVEL"
ENV_FORMAT = "AGENT_LOG_FORMAT"
ENV_CONTENT = "AGENT_LOG_CONTENT"
ENV_DELTAS = "AGENT_LOG_DELTAS"
ENV_MAX_BYTES = "AGENT_LOG_MAX_BYTES"
ENV_BACKUPS = "AGENT_LOG_BACKUPS"
ENV_CONSOLE = "AGENT_LOG_CONSOLE"

_TRUTHY = {"1", "true", "yes", "on", "y", "t"}


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in _TRUTHY


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _env_str(name: str, default: str) -> str:
    raw = os.environ.get(name)
    return default if raw is None or raw == "" else raw


@dataclass
class LogConfig:
    """Run-logger settings (file target, level, format, content, rotation)."""

    #: Directory the log file lives in (created on ``attach``).
    dir: str = "logs"
    #: File name inside :attr:`dir` (plain-text ``.log`` by default).
    file: str = "run.log"
    #: Explicit file path; when set it overrides :attr:`dir` / :attr:`file`.
    path: Optional[str] = None
    #: Minimum level written: ``debug`` / ``info`` / ``warning`` / ``error``.
    level: str = "info"
    #: Line format: ``"text"`` (human tail) or ``"json"`` (one JSON object/line).
    format: str = "text"
    #: When true, full (redacted) payloads are written; default is metadata only.
    include_content: bool = False
    #: When true, every ``*.delta`` event is written individually (debug only).
    log_deltas: bool = False
    #: Rotate once the file reaches this many bytes (0 disables rotation).
    max_bytes: int = 10 * 1024 * 1024
    #: Number of rotated backups to keep (``run.log.1`` … ``.N``).
    backups: int = 5
    #: Also mirror each record to stderr (human tail).
    console: bool = False
    #: Truncate any single string field longer than this many characters.
    max_field_bytes: int = 4096

    @property
    def resolved_path(self) -> str:
        """The actual file path (explicit ``path`` wins over ``dir``/``file``)."""
        if self.path:
            return os.path.abspath(os.path.expanduser(self.path))
        return os.path.abspath(os.path.join(os.path.expanduser(self.dir), self.file))

    @classmethod
    def from_env(cls) -> "LogConfig":
        """Build a config from ``AGENT_LOG_*`` (missing vars keep defaults)."""
        return cls(
            dir=_env_str(ENV_DIR, "logs"),
            file=_env_str(ENV_FILE, "run.log"),
            level=_env_str(ENV_LEVEL, "info").lower(),
            format=_env_str(ENV_FORMAT, "text").lower(),
            include_content=_env_bool(ENV_CONTENT, False),
            log_deltas=_env_bool(ENV_DELTAS, False),
            max_bytes=max(0, _env_int(ENV_MAX_BYTES, 10 * 1024 * 1024)),
            backups=max(0, _env_int(ENV_BACKUPS, 5)),
            console=_env_bool(ENV_CONSOLE, False),
        )
