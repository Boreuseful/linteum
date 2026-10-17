"""In-process storage bundle for tests and single-process demos."""

from __future__ import annotations

import time

from ..bus.artifact import InMemoryArtifactStore
from ..bus.wal import InMemoryWal
from ..runtime.state.checkpoint import InMemoryCheckpointer
from .base import SessionInfo, StorageBundle


class InMemorySessionCatalog:
    """Catalogue rows in a dict (same contract as the SQLite implementation)."""

    def __init__(self) -> None:
        self._sessions: dict[str, SessionInfo] = {}

    async def upsert_session(self, info: SessionInfo) -> None:
        current = self._sessions.get(info.session_id)
        if current is None:
            self._sessions[info.session_id] = info
            return
        self._sessions[info.session_id] = SessionInfo(
            session_id=info.session_id,
            created_at=current.created_at,
            updated_at=info.updated_at,
            phase=info.phase,
            round=info.round,
            message_count=info.message_count,
            last_seq=info.last_seq,
            title=info.title if info.title is not None else current.title,
            workspace=info.workspace if info.workspace is not None else current.workspace,
            metadata=dict(info.metadata or current.metadata),
        )

    async def get_session(self, session_id: str) -> SessionInfo | None:
        return self._sessions.get(session_id)

    async def list_sessions(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        workspace: str | None = None,
    ) -> list[SessionInfo]:
        rows = [
            info for info in self._sessions.values()
            if workspace is None or info.workspace == workspace
        ]
        rows.sort(key=lambda info: (info.updated_at, info.session_id), reverse=True)
        return rows[max(0, offset): max(0, offset) + max(1, limit)]

    async def delete_session(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)

    def touch(self, session_id: str, **changes) -> SessionInfo:
        current = self._sessions.get(session_id) or SessionInfo(
            session_id=session_id, created_at=time.time(), updated_at=time.time()
        )
        data = current.to_dict()
        data.update(changes)
        data["updated_at"] = time.time()
        info = SessionInfo.from_dict(data)
        self._sessions[session_id] = info
        return info


class InMemoryStorage:
    """Bundle of the four in-memory capabilities behind one object."""

    def __init__(self) -> None:
        self.wal = InMemoryWal()
        self.checkpointer = InMemoryCheckpointer()
        self.artifacts = InMemoryArtifactStore()
        self.catalog = InMemorySessionCatalog()

    @property
    def bundle(self) -> StorageBundle:
        return StorageBundle(
            wal=self.wal,
            checkpointer=self.checkpointer,
            artifacts=self.artifacts,
            catalog=self.catalog,
        )

    async def close(self) -> None:
        await self.wal.close()


__all__ = ["InMemorySessionCatalog", "InMemoryStorage"]
