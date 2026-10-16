"""Artifact store boundary for large-payload externalisation (§32.1).

The envelope's ``payload`` has a hard 256 KB cap, so anything larger must be
stored as a content-addressed blob and replaced by an ``art:<sha256>`` reference
*before* the event is committed (P5 / I12).  This module defines the narrow
interface the bus depends on plus a dependency-free in-memory implementation.

The real persistence layer (``storage/artifact.py`` in the target layout) will
implement the same protocol on top of the filesystem / object store; because the
bus only ever sees the protocol, swapping it later is transparent.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from .errors import ArtifactMissing, ArtifactUnavailable

ARTIFACT_PREFIX = "art:"


@dataclass(frozen=True, slots=True)
class Artifact:
    """Metadata for a stored blob (content-addressed -> inherently deduped)."""

    hash: str
    size: int
    mime: str
    session_id: str | None = None

    @property
    def ref(self) -> str:
        return f"{ARTIFACT_PREFIX}{self.hash}"


@runtime_checkable
class ArtifactStore(Protocol):
    """Minimal capability surface; ``available`` lets the bus fail fast (P5)."""

    available: bool

    async def put(self, data: bytes, *, mime: str = "application/octet-stream",
                  session_id: str | None = None) -> Artifact: ...

    async def get(self, hash: str) -> bytes: ...

    async def exists(self, hash: str) -> bool: ...


class InMemoryArtifactStore:
    """sha256-keyed in-memory store.  Identical bytes are stored once."""

    def __init__(self) -> None:
        self.available = True
        self._blobs: dict[str, bytes] = {}
        self._meta: dict[str, Artifact] = {}

    async def put(self, data: bytes, *, mime: str = "application/octet-stream",
                  session_id: str | None = None) -> Artifact:
        digest = hashlib.sha256(data).hexdigest()
        if digest not in self._blobs:
            self._blobs[digest] = data
            self._meta[digest] = Artifact(digest, len(data), mime, session_id)
        return self._meta[digest]

    async def get(self, hash: str) -> bytes:
        try:
            return self._blobs[hash]
        except KeyError as exc:
            raise ArtifactMissing(f"art:{hash} not found") from exc

    async def exists(self, hash: str) -> bool:
        return hash in self._blobs

    def __len__(self) -> int:
        return len(self._blobs)


class NullArtifactStore:
    """A store that refuses every write, forcing the bus's hard-limit failure.

    Used when no blob backend is configured: payloads up to ``INLINE_LIMIT``
    still flow, but an oversized payload raises ``E_ARTIFACT_UNAVAILABLE``
    instead of being silently truncated (Checklist #26).
    """

    available = False

    async def put(self, data: bytes, *, mime: str = "application/octet-stream",
                  session_id: str | None = None) -> Artifact:
        raise ArtifactUnavailable("no artifact store configured (payload too large)")

    async def get(self, hash: str) -> bytes:
        raise ArtifactMissing(f"art:{hash} unavailable: no artifact store configured")

    async def exists(self, hash: str) -> bool:
        return False


def resolve_ref(ref: str, *, artifacts: ArtifactStore) -> str:
    """Return the ``kind`` of a reference, validating its prefix (§32.2)."""
    for prefix in (ARTIFACT_PREFIX, "evt:", "mem:"):
        if ref.startswith(prefix):
            return prefix.rstrip(":")
    raise ArtifactMissing(f"unknown reference: {ref!r}")
