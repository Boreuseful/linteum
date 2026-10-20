"""Filesystem artifact store (§32.1).

The bus externalises payloads larger than ``INLINE_LIMIT`` before committing the
event, so the blob backend must be at least as durable as the WAL.  This store is
content-addressed: identical bytes are written once and referenced as
``art:<sha256>``.  Writes are atomic (temp file + ``os.replace``) so a crash can
never leave a half-written blob visible under its final hash.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import tempfile
import time
from pathlib import Path

from ..bus.artifact import Artifact, ArtifactMissing


class FileArtifactStore:
    """sha256-keyed blobs under ``root`` plus a small JSON metadata sidecar."""

    available = True

    def __init__(self, root: str | os.PathLike, *, fsync: bool = True) -> None:
        self.root = Path(root).expanduser().resolve()
        self.fsync = fsync
        self.root.mkdir(parents=True, exist_ok=True)

    async def put(self, data: bytes, *, mime: str = "application/octet-stream",
                  session_id: str | None = None) -> Artifact:
        return await asyncio.to_thread(self._put_sync, data, mime, session_id)

    async def get(self, hash: str) -> bytes:
        return await asyncio.to_thread(self._get_sync, hash)

    async def exists(self, hash: str) -> bool:
        return await asyncio.to_thread(self._exists_sync, hash)

    def get_meta(self, hash: str) -> Artifact | None:
        """Metadata sidecar (best effort; ``None`` when it is missing)."""
        path = self._meta_path(hash)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return Artifact(
            hash=hash,
            size=int(data.get("size", 0)),
            mime=data.get("mime", "application/octet-stream"),
            session_id=data.get("session_id"),
        )

    # -- paths --------------------------------------------------------------
    def _blob_path(self, hash: str) -> Path:
        return self.root / hash[:2] / hash

    def _meta_path(self, hash: str) -> Path:
        return self.root / hash[:2] / f"{hash}.json"

    # -- sync implementation ------------------------------------------------
    def _put_sync(self, data: bytes, mime: str, session_id: str | None) -> Artifact:
        digest = hashlib.sha256(data).hexdigest()
        blob = self._blob_path(digest)
        meta = self._meta_path(digest)
        blob.parent.mkdir(parents=True, exist_ok=True)
        if not blob.exists():
            self._atomic_write(blob, data)
        if not meta.exists():
            payload = json.dumps(
                {
                    "size": len(data),
                    "mime": mime,
                    "session_id": session_id,
                    "created": time.time(),
                },
                ensure_ascii=False,
                allow_nan=False,
            ).encode("utf-8")
            self._atomic_write(meta, payload)
        return Artifact(digest, len(data), mime, session_id)

    def _atomic_write(self, path: Path, data: bytes) -> None:
        fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                if self.fsync:
                    os.fsync(handle.fileno())
            os.replace(tmp, path)
        finally:
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass

    def _get_sync(self, hash: str) -> bytes:
        path = self._blob_path(hash)
        try:
            return path.read_bytes()
        except OSError as exc:
            raise ArtifactMissing(f"art:{hash} not found at {path}") from exc

    def _exists_sync(self, hash: str) -> bool:
        return self._blob_path(hash).is_file()


__all__ = ["FileArtifactStore"]
