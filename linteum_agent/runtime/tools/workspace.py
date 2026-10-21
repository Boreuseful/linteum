"""Workspace: confine file tools to a host-chosen root (§21–§24).

The design principle is simple: **the workspace path is a host capability, not
LLM input**.  A model may emit ``read_file(path="src/a.py")`` but it can never
choose *which* directory that is relative to — the host binds a
:class:`Workspace` into the file tools at assembly time (范式 A, closure binding),
and every path the model supplies passes through :meth:`Workspace.resolve`, the
only security boundary.

Three moving parts:

* :class:`Workspace` — a frozen value object holding a ``realpath``-normalised
  root plus policy (read-only, size caps, hidden-path allowance).
* :func:`make_file_tools` — the tool factory: ``read_file`` / ``write_file`` /
  ``edit_file`` / ``list_dir``, each closing over one ``Workspace``.  They are
  still "just functions of ``args``" (P3): the workspace is a closure variable,
  never an event payload.  Writes are size-capped and land atomically via a
  same-directory temp file + :func:`os.replace`.
* :class:`WorkspaceRegistry` / :class:`WorkspaceToolRegistries` — the host-side
  session → workspace / session → :class:`~linteum_agent.runtime.tools.registry.ToolRegistry`
  maps, so one process can serve many tenants without leaking across them.

Ordering matters in :meth:`Workspace.resolve`: the candidate is **resolved
(symlinks + ``..``) before the prefix assertion**, otherwise ``a/../../x`` or a
symlink pointing outside the root would slip through.  The threat model is a
misbehaving *model*, not a local attacker with write access to the root; the
TOCTOU window between resolve and I/O is accepted for Phase 1 (see the plan).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Optional

from .decorator import tool
from .errors import ToolExecutionError, WorkspaceUnavailable, WorkspaceViolation
from .registry import ToolRegistry
from .spec import ToolSpec

#: Default per-call size caps (1 MiB) — a read truncates, a write is rejected.
DEFAULT_MAX_READ_BYTES = 1 << 20
DEFAULT_MAX_WRITE_BYTES = 1 << 20

#: Friendly schema hint (NOT a security measure): reject a leading ``/`` and any
#: ``..`` path segment so the common traversals fail JSON-schema validation
#: before ever reaching :meth:`Workspace.resolve`.
_PATH_PATTERN = r"^(?!/)(?!.*(^|/)\.\.(/|$))"


# ─────────────────────────────────────────────────────────────────────────────
# Workspace (frozen value object)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Workspace:
    """An immutable, host-owned file root plus its access policy.

    ``root`` is normalised to an absolute ``realpath`` in :meth:`__post_init__`
    so later prefix comparisons are done against a canonical string.  The object
    itself can never be mutated by a tool (``frozen=True``).
    """

    root: Path
    session_id: str
    read_only: bool = False
    max_read_bytes: int = DEFAULT_MAX_READ_BYTES
    max_write_bytes: int = DEFAULT_MAX_WRITE_BYTES
    allow_hidden: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", Path(os.path.realpath(str(self.root))))

    # -- the guard ----------------------------------------------------------
    def resolve(self, rel: str, *, mode: str = "read") -> Path:
        """Resolve ``rel`` inside the root, or raise ``WorkspaceViolation``.

        ``mode`` is one of ``"read"`` / ``"write"`` / ``"list"``.  The returned
        path is always inside the (canonical) root.
        """
        if not isinstance(rel, str):
            raise WorkspaceViolation("path must be a string", kind="invalid_path")
        if not rel or "\x00" in rel:
            raise WorkspaceViolation("invalid path", kind="invalid_path")
        if mode not in ("read", "write", "list"):
            raise WorkspaceViolation(f"invalid mode: {mode!r}", kind="invalid_mode")
        if mode == "write" and self.read_only:
            raise WorkspaceViolation("workspace is read-only", kind="read_only")

        if rel.startswith("~"):
            raise WorkspaceViolation("absolute paths are not allowed",
                                     kind="absolute_path", path=rel)
        if Path(rel).is_absolute():
            raise WorkspaceViolation("absolute paths are not allowed",
                                     kind="absolute_path", path=rel)

        root = self.root
        if not root.is_dir() or not os.access(root, os.R_OK):
            raise WorkspaceUnavailable("workspace root is unavailable",
                                       session_id=self.session_id)

        try:
            candidate = (root / rel).resolve(strict=False)
        except (OSError, RuntimeError):
            # symlink loop / unreadable component: never let str(exc) leak a path
            raise WorkspaceViolation("invalid path", kind="invalid_path", path=rel) from None

        norm_root = os.path.normcase(str(root))
        norm_candidate = os.path.normcase(str(candidate))
        if norm_candidate != norm_root and not norm_candidate.startswith(norm_root + os.sep):
            raise WorkspaceViolation(f"path escapes workspace: {rel!r}",
                                     kind="escape", path=rel)

        if not self.allow_hidden:
            parts = candidate.relative_to(root).parts
            if any(part.startswith(".") for part in parts):
                raise WorkspaceViolation(f"hidden path not allowed: {rel!r}",
                                         kind="hidden", path=rel)

        if candidate.exists():
            if candidate.is_dir() and mode != "list":
                raise WorkspaceViolation("target is a directory",
                                         kind="is_directory", path=rel)
            if mode != "list" and not candidate.is_file():
                raise WorkspaceViolation("target is not a regular file",
                                         kind="not_regular_file", path=rel)

        return candidate

    # -- helpers ------------------------------------------------------------
    def relative(self, path) -> str:
        """Render ``path`` relative to the root — the value tools may return.

        If (against expectation) ``path`` is not under the root, fall back to the
        base name so an absolute path can never be exposed.
        """
        try:
            rel = Path(path).relative_to(self.root)
        except ValueError:
            return Path(path).name
        return str(rel) if str(rel) else "."


# ─────────────────────────────────────────────────────────────────────────────
# I/O helpers (all OSError -> WorkspaceViolation with a relative path only)
# ─────────────────────────────────────────────────────────────────────────────

def _atomic_write(target: Path, data: bytes) -> None:
    """Write ``data`` to a same-directory temp file, then atomically replace.

    Same-directory placement keeps :func:`os.replace` on one filesystem (no
    cross-device ``EXDEV``) and guarantees a reader never sees a half-written
    file.
    """
    directory = str(target.parent)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=directory)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)
    except OSError:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


# ─────────────────────────────────────────────────────────────────────────────
# Tool factory (范式 A: the workspace is closed over, never an event)
# ─────────────────────────────────────────────────────────────────────────────

def make_file_tools(ws: Workspace) -> list[ToolSpec]:
    """Build the workspace-confined file tools bound to ``ws``.

    All tools return **relative** paths and convert every ``OSError`` into a
    :class:`WorkspaceViolation` whose text contains only the relative path.
    Writes/edits are ``side_effect="write"`` (→ approval by default),
    ``idempotent=False`` and ``concurrency=1``.
    """
    path_schema = {"type": "string", "pattern": _PATH_PATTERN}

    @tool(
        "Read a UTF-8 text file (path is relative to the workspace root).",
        {"path": path_schema}, required=["path"], side_effect="read",
        timeout=10, name="read_file",
    )
    def read_file(path: str) -> str:
        target = ws.resolve(path, mode="read")
        try:
            with open(target, "rb") as handle:          # bounded: read at most N bytes
                data = handle.read(ws.max_read_bytes)
        except OSError:
            raise WorkspaceViolation("failed to read file", path=path, kind="io_error") from None
        return data.decode("utf-8", "replace")

    @tool(
        "Overwrite a UTF-8 text file (path is relative to the workspace root).",
        {"path": path_schema, "text": {"type": "string"}},
        required=["path", "text"], side_effect="write", idempotent=False,
        concurrency=1, timeout=30, name="write_file",
    )
    def write_file(path: str, text: str) -> str:
        target = ws.resolve(path, mode="write")
        data = text.encode("utf-8")
        if len(data) > ws.max_write_bytes:
            raise WorkspaceViolation("content exceeds max_write_bytes",
                                     path=path, kind="too_large")
        try:
            _atomic_write(target, data)
        except OSError:
            raise WorkspaceViolation("failed to write file", path=path, kind="io_error") from None
        return ws.relative(target)

    @tool(
        "Replace the first occurrence of `old` with `new` in a text file.",
        {"path": path_schema, "old": {"type": "string"}, "new": {"type": "string"}},
        required=["path", "old", "new"], side_effect="write", idempotent=False,
        concurrency=1, timeout=30, name="edit_file",
    )
    def edit_file(path: str, old: str, new: str) -> str:
        target = ws.resolve(path, mode="write")
        try:
            size = target.stat().st_size
        except OSError:
            raise WorkspaceViolation("failed to read file", path=path, kind="io_error") from None
        if size > ws.max_read_bytes:
            raise WorkspaceViolation("file exceeds max_read_bytes",
                                     path=path, kind="too_large")
        try:
            with open(target, "rb") as handle:
                body = handle.read(ws.max_read_bytes + 1).decode("utf-8", "replace")
        except OSError:
            raise WorkspaceViolation("failed to read file", path=path, kind="io_error") from None
        if old not in body:
            raise WorkspaceViolation("`old` not found", path=path, kind="not_found")
        data = body.replace(old, new, 1).encode("utf-8")
        if len(data) > ws.max_write_bytes:
            raise WorkspaceViolation("content exceeds max_write_bytes",
                                     path=path, kind="too_large")
        try:
            _atomic_write(target, data)
        except OSError:
            raise WorkspaceViolation("failed to write file", path=path, kind="io_error") from None
        return ws.relative(target)

    @tool(
        "List a directory (path is relative to the workspace root; default '.').",
        {"path": path_schema}, required=[], side_effect="read",
        timeout=10, name="list_dir",
    )
    def list_dir(path: str = ".") -> list:
        target = ws.resolve(path, mode="list")
        try:
            names = os.listdir(target)
        except OSError:
            raise WorkspaceViolation("failed to list directory", path=path, kind="io_error") from None
        entries: list = []
        for name in sorted(names):
            if not ws.allow_hidden and name.startswith("."):
                continue
            child = target / name
            if os.path.islink(child):
                kind = "link"
            elif child.is_dir():
                kind = "dir"
            else:
                kind = "file"
            entries.append({"name": name, "type": kind})
        return entries

    return [
        read_file.__tool_spec__,
        write_file.__tool_spec__,
        edit_file.__tool_spec__,
        list_dir.__tool_spec__,
    ]


# ─────────────────────────────────────────────────────────────────────────────
# bash 工具（工作区受限 + 危险副作用）
# ─────────────────────────────────────────────────────────────────────────────

#: Default wall-clock cap and per-stream capture cap for the bash tool.
DEFAULT_BASH_TIMEOUT = 30.0
DEFAULT_BASH_MAX_OUTPUT = 64 * 1024
#: Environment variables passed through to the shell — a **minimal allow-list** so
#: the parent's secrets (``*_API_KEY`` …) never leak to a command like ``env``.
BASH_ENV_ALLOW: tuple = ("PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "TMPDIR")


def _clip_output(data: bytes, limit: int) -> str:
    text = data[:limit].decode("utf-8", "replace")
    if len(data) > limit:
        text += f"\n…(output truncated, {len(data)} bytes total)"
    return text


def _kill_group(proc) -> None:
    """Kill the whole process group (``start_new_session``) so children don't linger."""
    if proc.returncode is not None:
        return
    with contextlib.suppress(ProcessLookupError, OSError):
        os.killpg(proc.pid, signal.SIGKILL)          # pgid == pid (start_new_session)


def make_bash_tool(ws: Workspace, *, timeout: Optional[float] = None,
                   max_output: Optional[int] = None,
                   env_allow: Optional[tuple] = None) -> ToolSpec:
    """Build a workspace-confined ``bash`` tool bound to ``ws`` (范式 A).

    Safety properties (see the plan's threat model — this defends against a
    misbehaving *model*, not a local attacker):

    * ``side_effect="dangerous"`` → ``requires_approval=True``: a human sees the
      full command before it runs;
    * ``cwd`` is fixed to the workspace root (an optional relative subdirectory
      must pass :meth:`Workspace.resolve`), so ``..``/absolute/symlink escapes in
      the *``cwd`` argument* are rejected;
    * only a **minimal environment** is passed, so the parent's API keys do not
      leak to the command;
    * ``concurrency=1``; ``timeout`` bounds the call; on timeout/cancel the whole
      process group is killed;
    * the result's ``cwd`` is **relative** — an absolute path is never echoed.

    **Caveat (by design):** a shell command can still traverse the filesystem
    (``cd ..``, ``cat /etc/passwd``) — a path guard cannot constrain an arbitrary
    command.  Depth comes from *cwd confinement + mandatory approval + minimal
    environment*; strong isolation needs a container (L3), which is not
    implemented.
    """
    limit = DEFAULT_BASH_MAX_OUTPUT if max_output is None else max_output
    wall_clock = DEFAULT_BASH_TIMEOUT if timeout is None else timeout
    allow = BASH_ENV_ALLOW if env_allow is None else tuple(env_allow)

    def _env() -> dict:
        return {key: os.environ[key] for key in allow if key in os.environ}

    @tool(
        "Run a bash command inside the workspace (cwd is the workspace root; "
        "dangerous → human approval required).",
        {"command": {"type": "string", "description": "the shell command to run"},
         "cwd": {"type": "string", "description": "subdirectory relative to the workspace root (default '.')"}},
        required=["command"], side_effect="dangerous", concurrency=1,
        timeout=wall_clock, name="bash",
    )
    async def bash(command: str, cwd: str = ".") -> dict:
        if not isinstance(command, str) or not command.strip():
            raise WorkspaceViolation("bash command must not be empty", kind="invalid_command")
        if ws.read_only:
            raise WorkspaceViolation("workspace is read-only", kind="read_only")
        workdir = ws.resolve(cwd, mode="list")       # must exist, be a dir, and stay inside root

        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                cwd=str(workdir),
                env=_env(),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
        except OSError:
            raise ToolExecutionError("failed to start bash") from None

        try:
            stdout_b, stderr_b = await proc.communicate()   # outer asyncio.timeout cancels here
        except BaseException:                                # timeout / cancel / error: kill then re-raise
            _kill_group(proc)
            raise

        return {
            "returncode": proc.returncode,
            "stdout": _clip_output(stdout_b or b"", limit),
            "stderr": _clip_output(stderr_b or b"", limit),
            "cwd": ws.relative(workdir),
        }

    return bash.__tool_spec__


# ─────────────────────────────────────────────────────────────────────────────
# Host-side session maps
# ─────────────────────────────────────────────────────────────────────────────

class WorkspaceRegistry:
    """Lazily map ``session_id`` → :class:`Workspace` via a host factory.

    The factory is the *only* place that knows "whose workspace is which
    directory"; it must translate host identity (user / project / tenant) into a
    :class:`Workspace`.  That mapping never enters ``AgentState`` (which is
    replayable state; absolute paths are neither portable nor safe there).
    """

    def __init__(self, factory: Callable[[str], Workspace]) -> None:
        self._factory = factory
        self._cache: dict[str, Workspace] = {}

    def for_session(self, session_id: str) -> Workspace:
        ws = self._cache.get(session_id)
        if ws is None:
            ws = self._factory(session_id)
            self._cache[session_id] = ws
        return ws

    def drop(self, session_id: str) -> None:
        """Discard the cached workspace for a closed/deleted session."""
        self._cache.pop(session_id, None)

    def __contains__(self, session_id: object) -> bool:
        return session_id in self._cache

    def __len__(self) -> int:
        return len(self._cache)


class WorkspaceToolRegistries:
    """Build and cache one :class:`~linteum_agent.runtime.tools.registry.ToolRegistry` per session.

    Each session's registry carries the file tools bound to *that* session's
    :class:`Workspace`, so ``ToolRuntime(registry_for=registries.for_session)``
    selects the right tool set purely from ``ev.session_id``.

    ``register_many`` is async, so :meth:`for_session` is async too; a host that
    needs an :class:`~linteum_agent.runtime.agent.Agent` schema provider (a *sync* callable) can
    prime it once with ``await registries.for_session(sid)`` and then pass the
    returned registry's ``schemas``.
    """

    def __init__(self, workspaces: WorkspaceRegistry, *,
                 extra_specs: Iterable[ToolSpec] = (),
                 factory: Optional[Callable[[Workspace], Iterable[ToolSpec]]] = None) -> None:
        self.workspaces = workspaces
        self._extra = tuple(extra_specs)
        #: Host hook: build the workspace-bound tool batch (file tools by default,
        #: plus e.g. ``bash`` when the host wants it).  Called once per session.
        self._factory = factory or make_file_tools
        self._registries: dict[str, ToolRegistry] = {}
        self._lock = asyncio.Lock()

    async def for_session(self, session_id: str) -> ToolRegistry:
        registry = self._registries.get(session_id)
        if registry is not None:
            return registry
        async with self._lock:
            registry = self._registries.get(session_id)
            if registry is None:
                ws = self.workspaces.for_session(session_id)
                registry = ToolRegistry()
                await registry.register_many([*self._extra, *self._factory(ws)])
                self._registries[session_id] = registry
        return registry

    def install(self, session_id: str, registry: ToolRegistry) -> None:
        """Install an already-built registry (e.g. a filtered subagent table).

        Without this, ``for_session`` would rebuild the full workspace tool set
        through the factory and silently undo the filter (design §7.4).
        """
        self._registries[session_id] = registry

    def built(self, session_id: str) -> Optional[ToolRegistry]:
        """Sync accessor for a registry already built via :meth:`for_session`."""
        return self._registries.get(session_id)

    def drop(self, session_id: str) -> None:
        """Forget a session's registry (session eviction / deletion)."""
        self._registries.pop(session_id, None)

    def __len__(self) -> int:
        return len(self._registries)
