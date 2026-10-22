"""Storage boundaries for durable sessions (§14.2, §18.3, §47).

The runtime already defines the three storage capabilities it needs:

* :class:`~linteum_agent.bus.wal.WalStore` — the append-only event log;
* :class:`~linteum_agent.runtime.state.checkpoint.Checkpointer` — state snapshots;
* :class:`~linteum_agent.bus.artifact.ArtifactStore` — content-addressed blobs.

This module adds the one read-model capability the host needs — session metadata
for listing and opening conversations — and a bundle that wires the four
capabilities together.  Implementations stay swappable: SQLite is the first
backend, but nothing above this boundary depends on it.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Protocol, runtime_checkable

from ..bus.artifact import ArtifactStore
from ..bus.wal import WalStore
from ..runtime.state.checkpoint import Checkpointer


@dataclass(frozen=True, slots=True)
class SessionInfo:
    """A session catalogue row (metadata only, never message content)."""

    session_id: str
    created_at: float
    updated_at: float
    phase: str = "idle"
    round: int = 0
    message_count: int = 0
    last_seq: int = 0
    title: str | None = None
    workspace: str | None = None
    metadata: dict = field(default_factory=dict)
    #: Parent session for a delegated child (``None`` for root sessions).
    parent_session_id: str | None = None
    #: ``root`` (user-created) or ``subagent`` (orchestrator-created).
    kind: str = "root"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "SessionInfo":
        return cls(
            session_id=str(data["session_id"]),
            created_at=float(data.get("created_at", 0.0)),
            updated_at=float(data.get("updated_at", 0.0)),
            phase=data.get("phase", "idle"),
            round=int(data.get("round", 0)),
            message_count=int(data.get("message_count", 0)),
            last_seq=int(data.get("last_seq", 0)),
            title=data.get("title"),
            workspace=data.get("workspace"),
            metadata=dict(data.get("metadata") or {}),
            parent_session_id=data.get("parent_session_id"),
            kind=data.get("kind", "root"),
        )


@runtime_checkable
class SessionCatalog(Protocol):
    """Session metadata surface used by :class:`SessionService`."""

    async def upsert_session(self, info: SessionInfo) -> None: ...

    async def get_session(self, session_id: str) -> SessionInfo | None: ...

    async def list_sessions(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        workspace: str | None = None,
        include_subagents: bool = False,
        parent_session_id: str | None = None,
    ) -> list[SessionInfo]: ...

    async def list_children(
        self,
        parent_session_id: str,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> list[SessionInfo]: ...

    async def delete_session(self, session_id: str) -> None: ...


@dataclass(frozen=True, slots=True)
class StorageBundle:
    """The four storage capabilities a host assembles once per process."""

    wal: WalStore
    checkpointer: Checkpointer
    artifacts: ArtifactStore
    catalog: SessionCatalog


__all__ = [
    "SessionInfo",
    "SessionCatalog",
    "StorageBundle",
]
