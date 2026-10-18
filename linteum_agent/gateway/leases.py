"""Cross-process session leases (§6.1).

``InMemoryEventBus.seq`` is process-local.  Two processes writing the same
``session_id`` would allocate overlapping ``seq`` values and SQLite's
``UNIQUE(session_id, seq)`` + ``INSERT OR IGNORE`` would silently discard the
loser's events.  A lease therefore enforces **one live reducer per session**
across processes:

* the owner writes a small JSON file (``pid``, host, heartbeat, endpoint);
* a heartbeat is renewed every ``ttl / 3`` seconds;
* a lease is stale when the heartbeat is too old or the owning pid is gone;
* a stale lease is taken over atomically under a per-session ``flock``.

Non-owners degrade to read-only history (or attach to the owner's endpoint);
they must never start an ``Agent`` for that session.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import re
import socket
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

#: Default heartbeat budget in seconds.  A lease older than this is stale.
DEFAULT_LEASE_TTL = 30.0
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def default_lease_root() -> Path:
    """``$LINTEUM_HOME/leases`` (same rule as ``config.paths.linteum_home``).

    Kept local so importing the client SDK does not pull in the config/runtime
    packages; the lease manager is only constructed by a host.
    """
    override = os.environ.get("LINTEUM_HOME")
    home = Path(override).expanduser() if override and override.strip() \
        else Path.home() / ".linteum"
    return home / "leases"


class LeaseError(RuntimeError):
    """Base class for lease failures."""


class LeaseHeld(LeaseError):
    """Another live process owns the session."""

    def __init__(self, lease: "Lease") -> None:
        super().__init__(
            f"session {lease.session_id!r} is owned by pid {lease.pid} "
            f"on {lease.host} (endpoint={lease.endpoint!r})"
        )
        self.lease = lease


@dataclass(frozen=True, slots=True)
class Lease:
    """The persisted ownership record for one session."""

    session_id: str
    pid: int
    host: str
    endpoint: Optional[str] = None
    acquired_at: float = 0.0
    heartbeat_at: float = 0.0
    #: Random per-process identity.  ``pid`` alone is unsafe because pids are
    #: reused; ownership checks prefer this value when it is present.
    instance_id: str = ""

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "pid": self.pid,
            "host": self.host,
            "endpoint": self.endpoint,
            "acquired_at": self.acquired_at,
            "heartbeat_at": self.heartbeat_at,
            "instance_id": self.instance_id,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Lease":
        return cls(
            session_id=str(data["session_id"]),
            pid=int(data.get("pid", 0)),
            host=str(data.get("host", "")),
            endpoint=data.get("endpoint"),
            acquired_at=float(data.get("acquired_at", 0.0)),
            heartbeat_at=float(data.get("heartbeat_at", 0.0)),
            instance_id=str(data.get("instance_id", "")),
        )

    def owned_by(self, pid: int, instance_id: str = "") -> bool:
        if self.instance_id and instance_id:
            return self.instance_id == instance_id
        return self.pid == pid

    def is_stale(self, ttl: float = DEFAULT_LEASE_TTL, *,
                 now: Optional[float] = None,
                 host: Optional[str] = None) -> bool:
        """True when the owner is gone (dead pid or expired heartbeat)."""
        current = time.time() if now is None else now
        if current - self.heartbeat_at > ttl:
            return True
        local_host = socket.gethostname() if host is None else host
        if self.host and self.host == local_host and self.pid > 0:
            try:
                os.kill(self.pid, 0)
            except ProcessLookupError:
                return True
            except (PermissionError, OSError):
                pass
        return False


class FileLeaseManager:
    """Single-writer leases backed by one JSON file per session.

    The manager is deliberately small and synchronous underneath: acquisition
    and heartbeat run in a worker thread so the event loop never blocks on
    ``flock``/fsync.
    """

    def __init__(
        self,
        root: str | os.PathLike | None = None,
        *,
        ttl: float = DEFAULT_LEASE_TTL,
        pid: Optional[int] = None,
        hostname: Optional[str] = None,
        instance_id: Optional[str] = None,
        clock=time.time,
    ) -> None:
        if ttl <= 0:
            raise ValueError("lease ttl must be positive")
        self.root = Path(root).expanduser() if root is not None \
            else default_lease_root()
        self.root.mkdir(parents=True, exist_ok=True)
        self.ttl = float(ttl)
        self.pid = os.getpid() if pid is None else int(pid)
        self.host = hostname or socket.gethostname()
        self.instance_id = instance_id or uuid.uuid4().hex
        self._clock = clock
        self._lock = threading.RLock()
        self._held: dict[str, Lease] = {}

    @property
    def heartbeat_interval(self) -> float:
        return max(1.0, self.ttl / 3.0)

    # -- naming -------------------------------------------------------------
    def _stem(self, session_id: str) -> str:
        safe = _UNSAFE.sub("_", session_id)[:48] or "session"
        digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:16]
        return f"{safe}-{digest}"

    def lease_path(self, session_id: str) -> Path:
        return self.root / f"{self._stem(session_id)}.json"

    def lock_path(self, session_id: str) -> Path:
        return self.root / f"{self._stem(session_id)}.lock"

    # -- public API ---------------------------------------------------------
    async def acquire(self, session_id: str, *,
                      endpoint: Optional[str] = None) -> Lease:
        """Acquire (or take over) the session lease; raises :class:`LeaseHeld`."""
        return await asyncio.to_thread(self._acquire_sync, session_id, endpoint)

    async def heartbeat(self, session_id: str) -> Optional[Lease]:
        return await asyncio.to_thread(self._heartbeat_sync, session_id)

    def release(self, session_id: str) -> None:
        """Release the lease if this process owns it (sync, idempotent)."""
        self._release_sync(session_id)

    async def owner(self, session_id: str) -> Optional[Lease]:
        lease = await asyncio.to_thread(self._read_sync, session_id)
        if lease is None or lease.is_stale(self.ttl, host=self.host):
            return None
        return lease

    async def raw_owner(self, session_id: str) -> Optional[Lease]:
        """Read the record even when stale (diagnostics)."""
        return await asyncio.to_thread(self._read_sync, session_id)

    def held(self) -> tuple:
        return tuple(self._held.values())

    # -- implementation -----------------------------------------------------
    def _acquire_sync(self, session_id: str, endpoint: Optional[str]) -> Lease:
        with self._lock, self._flock(self.lock_path(session_id)):
            current = self._read_sync(session_id)
            if current is not None and not current.owned_by(self.pid, self.instance_id):
                if not current.is_stale(self.ttl, now=self._clock(), host=self.host):
                    raise LeaseHeld(current)
            now = self._clock()
            lease = Lease(
                session_id=session_id,
                pid=self.pid,
                host=self.host,
                endpoint=endpoint,
                acquired_at=now,
                heartbeat_at=now,
                instance_id=self.instance_id,
            )
            self._write_sync(session_id, lease)
            self._held[session_id] = lease
            return lease

    def _heartbeat_sync(self, session_id: str) -> Optional[Lease]:
        with self._lock, self._flock(self.lock_path(session_id)):
            current = self._read_sync(session_id)
            if current is None or not current.owned_by(self.pid, self.instance_id):
                self._held.pop(session_id, None)
                return None
            lease = Lease(
                session_id=current.session_id,
                pid=current.pid,
                host=current.host,
                endpoint=current.endpoint,
                acquired_at=current.acquired_at,
                heartbeat_at=self._clock(),
                instance_id=current.instance_id,
            )
            self._write_sync(session_id, lease)
            self._held[session_id] = lease
            return lease

    def _release_sync(self, session_id: str) -> None:
        with self._lock, self._flock(self.lock_path(session_id)):
            current = self._read_sync(session_id)
            if current is not None and current.owned_by(self.pid, self.instance_id):
                with contextlib.suppress(OSError):
                    self.lease_path(session_id).unlink()
            self._held.pop(session_id, None)

    def _read_sync(self, session_id: str) -> Optional[Lease]:
        path = self.lease_path(session_id)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return None
        if not isinstance(data, dict):
            return None
        try:
            return Lease.from_dict(data)
        except (KeyError, TypeError, ValueError):
            return None

    def _write_sync(self, session_id: str, lease: Lease) -> None:
        path = self.lease_path(session_id)
        payload = json.dumps(
            lease.to_dict(), ensure_ascii=False, allow_nan=False, sort_keys=True
        ).encode("utf-8")
        fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            with contextlib.suppress(OSError):
                os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        finally:
            with contextlib.suppress(OSError):
                if tmp.exists():
                    tmp.unlink()

    @contextlib.contextmanager
    def _flock(self, path: Path):
        """Serialise acquisition/takeover across processes (POSIX)."""
        handle = open(path, "a+b")
        try:
            try:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            except (ImportError, OSError):  # pragma: no cover - non-POSIX
                pass
            yield
        finally:
            try:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except (ImportError, OSError):  # pragma: no cover
                pass
            handle.close()


__all__ = [
    "DEFAULT_LEASE_TTL",
    "FileLeaseManager",
    "Lease",
    "LeaseError",
    "LeaseHeld",
    "default_lease_root",
]
