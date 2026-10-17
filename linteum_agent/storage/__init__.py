"""Durable session storage (WAL + checkpoints + artifact blobs).

Public surface:

* :class:`~linteum_agent.storage.base.SessionInfo` / ``StorageBundle``;
* :class:`~linteum_agent.storage.sqlite.SqliteStorage` — SQLite default backend;
* :class:`~linteum_agent.storage.artifacts.FileArtifactStore` — content-addressed
  blobs for oversized payloads and summaries;
* :class:`~linteum_agent.storage.memory.InMemoryStorage` — tests;
* :class:`~linteum_agent.storage.service.SessionService` — history/context reads.
"""

from .artifacts import FileArtifactStore
from .base import SessionCatalog, SessionInfo, StorageBundle
from .memory import InMemorySessionCatalog, InMemoryStorage
from .service import SessionService
from .sqlite import SqliteStorage

__all__ = [
    "FileArtifactStore",
    "InMemorySessionCatalog",
    "InMemoryStorage",
    "SessionCatalog",
    "SessionInfo",
    "SessionService",
    "SqliteStorage",
    "StorageBundle",
]
