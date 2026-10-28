"""Per-conversation options.

The semi-permanent :class:`~linteum_agent.config.schema.AppConfig` deliberately
does **not** contain ``workspace``: different conversations routinely operate on
different directories.  The workspace is therefore supplied when a session is
created (``session.create {workspace}``) and persisted on the session row
(``SessionInfo.workspace``); it never enters the configuration file.

``SessionOptions`` is the validated value object intended for the CLI/TUI/Web
gateway and ``AgentHost.open_session``.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional

from .schema import ConfigError

_SESSION_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_PROFILE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_MODE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_MAX_TITLE = 512


@dataclass(frozen=True, slots=True)
class SessionOptions:
    """Everything that is specific to one conversation.

    ``workspace`` is required and must be an existing directory.  Relative
    paths are resolved against the process working directory; ``~`` is
    expanded.  The resolved absolute path is what gets persisted and bound to
    the session's tool registry.
    """

    workspace: Path | str
    session_id: Optional[str] = None
    profile: str = "default"
    mode: Optional[str] = None
    title: Optional[str] = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        path = Path(self.workspace).expanduser()
        if not path.is_absolute():
            path = path.resolve()
        path = path.resolve()
        if not path.is_dir():
            raise ConfigError(
                f"workspace is not an existing directory: {path}",
                key="workspace",
                code="E_SESSION_WORKSPACE",
                hint="pass an absolute directory path when creating the session",
            )
        object.__setattr__(self, "workspace", path)

        if self.session_id is not None and not _SESSION_ID.match(self.session_id):
            raise ConfigError(
                f"invalid session_id: {self.session_id!r}",
                key="session_id",
                code="E_SESSION_ID",
                hint="use 1-128 characters from [A-Za-z0-9._:-]",
            )
        if not _PROFILE.match(self.profile):
            raise ConfigError(
                f"invalid profile name: {self.profile!r}",
                key="profile",
                code="E_SESSION_PROFILE",
                hint="use 1-64 characters from [A-Za-z0-9._-]",
            )
        if self.mode is not None and not _MODE.match(self.mode):
            raise ConfigError(
                f"invalid mode name: {self.mode!r}",
                key="mode",
                code="E_SESSION_MODE",
                hint="use 1-64 characters from [a-z0-9._-], starting with a letter or digit",
            )
        if self.title is not None:
            if not isinstance(self.title, str) or not self.title.strip():
                raise ConfigError("title must be a non-empty string",
                                  key="title", code="E_SESSION_TITLE")
            if len(self.title) > _MAX_TITLE:
                raise ConfigError(f"title must be <= {_MAX_TITLE} characters",
                                  key="title", code="E_SESSION_TITLE")
        if not isinstance(self.metadata, Mapping):
            raise ConfigError("metadata must be a mapping",
                              key="metadata", code="E_SESSION_METADATA")
        try:
            json.dumps(dict(self.metadata), ensure_ascii=False)
        except (TypeError, ValueError) as exc:
            raise ConfigError(
                f"metadata must be JSON-serialisable: {exc}",
                key="metadata", code="E_SESSION_METADATA",
            ) from exc

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "workspace": str(self.workspace),
            "profile": self.profile,
            "mode": self.mode,
            "title": self.title,
            "metadata": dict(self.metadata),
        }


__all__ = ["SessionOptions"]
