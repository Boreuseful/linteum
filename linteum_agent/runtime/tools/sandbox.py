"""Sandbox: run a tool by *location*, not by identity (§23).

"位置无关" (G7) — the same ``ToolRuntime`` drives a pure in-process function and a
subprocess worker without knowing the difference.  This module defines the
:class:`Sandbox` façade plus the backends of the §23 ladder:

===========  ======================================  =======================
 level        backend                                 cancellation
===========  ======================================  =======================
 L0 in-proc   :class:`InProcessBackend`               ``CancelToken`` (coop.)
 L1 thread    :class:`ThreadBackend`                  not interruptible (warn)
 L2 sub-proc  :class:`SubprocessBackend`              ``p.kill()``
 L3/L4        pluggable (container / MCP / gRPC)     销毁 / 关闭流
===========  ======================================  =======================

Every backend exposes ``invoke(spec, args, token, progress, *, call_id)`` and
``kill(call_id)``.  ``progress`` is an **async** callable the tool may use to
stream partial output; the runtime coalesces those into ``tool.output.delta``
events, which the bus then merges on 50 ms / 4 KB windows (§13.1, Checklist #9).

The L2 backend speaks a tiny newline-delimited JSON protocol (see
:mod:`linteum_agent.runtime.tools.worker`); the *worker* side batches progress into ≤4 KB frames,
so a chatty worker cannot flood the bus one line at a time.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Optional, Protocol, runtime_checkable

from ...bus.scheduler.cancel import CancelToken
from .errors import SandboxError, ToolExecutionError

#: Project root, injected into a subprocess' ``PYTHONPATH`` so ``agent`` imports.
_PACKAGE_ROOT = str(Path(__file__).resolve().parents[3])


async def _noop_progress(payload: dict) -> None:  # pragma: no cover - trivial
    return None


@runtime_checkable
class WorkerBackend(Protocol):
    """Where a tool actually runs (§23/§53)."""

    async def invoke(
        self,
        spec: Any,
        args: dict,
        token: CancelToken,
        progress: Callable[[dict], Any],
        *,
        call_id: Optional[str] = None,
    ) -> Any: ...

    async def kill(self, call_id: str) -> None: ...


class BaseBackend:
    """Shared default: backends without a killable resource are no-ops."""

    async def kill(self, call_id: str) -> None:  # noqa: D401 - trivial
        return None


class InProcessBackend(BaseBackend):
    """L0 — ``await spec.fn(**args)`` in this event loop (§23).

    Cheapest and fastest; for pure/read tools.  A tool that declares a
    ``progress`` parameter is handed the async reporter (streaming in-proc tool).
    Cancellation is cooperative: an async tool observes the token itself, and a
    synchronous tool cannot be interrupted (it runs to completion).
    """

    async def invoke(self, spec, args, token, progress, *, call_id=None):
        token.raise_if_cancelled()
        kwargs = dict(args)
        if spec.accepts_progress():
            kwargs["progress"] = progress
        result = spec.fn(**kwargs)
        if hasattr(result, "__await__"):
            result = await result
        token.raise_if_cancelled()
        return result


class ThreadBackend(BaseBackend):
    """L1 — run a blocking callable in a thread pool (§23).

    For tools that call blocking C extensions.  A running thread cannot be
    interrupted; cancellation is only observed before/after (the design accepts
    this and logs a warning — Checklist #7).
    """

    def __init__(self, max_workers: int = 4) -> None:
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="tool")

    async def invoke(self, spec, args, token, progress, *, call_id=None):
        token.raise_if_cancelled()
        loop = asyncio.get_running_loop()
        # ``progress`` is omitted on purpose: it is an async reporter and would
        # be unsafe to await from a foreign thread.
        result = await loop.run_in_executor(self._executor, lambda: spec.fn(**args))
        token.raise_if_cancelled()
        return result


class SubprocessBackend(BaseBackend):
    """L2 — run an *importable* tool in a child ``python`` process (§23).

    The parent frames one JSON request on the worker's stdin; the worker streams
    newline-delimited JSON frames back (``progress`` / ``final``).  A chatty
    worker is batched by the worker side (see :mod:`linteum_agent.runtime.tools.worker`), and
    cancelling the token kills the process outright (``p.kill()``).
    """

    def __init__(self) -> None:
        self.procs: dict[str, asyncio.subprocess.Process] = {}

    @staticmethod
    def _env() -> dict:
        env = dict(os.environ)
        existing = env.get("PYTHONPATH")
        env["PYTHONPATH"] = _PACKAGE_ROOT + (os.pathsep + existing if existing else "")
        return env

    async def invoke(self, spec, args, token, progress, *, call_id=None):
        entrypoint = spec.qualified_name
        if "<locals>" in entrypoint:
            raise SandboxError(
                f"tool {spec.name!r} is not importable out-of-process "
                f"({entrypoint}); move it to module scope or set entrypoint="
            )
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "linteum_agent.runtime.tools.worker",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self._env(),
                cwd=_PACKAGE_ROOT,
            )
        except OSError as exc:  # pragma: no cover - environment dependent
            raise SandboxError(f"failed to start worker: {exc}") from exc

        if call_id is not None:
            self.procs[call_id] = proc
        watcher = asyncio.create_task(self._watch_cancel(token, proc))
        try:
            request = json.dumps({"entrypoint": entrypoint, "args": args}).encode("utf-8")
            proc.stdin.write(request)
            await proc.stdin.drain()
            proc.stdin.close()

            final: Optional[dict] = None
            async for raw in proc.stdout:
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                try:
                    frame = json.loads(line)
                except json.JSONDecodeError:  # tolerate stray output
                    continue
                kind = frame.get("kind")
                if kind == "progress":
                    await progress(frame.get("data") or {})
                elif kind == "final":
                    final = frame

            rc = await proc.wait()
            if final is None:
                stderr = (await proc.stderr.read()).decode("utf-8", "replace")
                raise SandboxError(f"worker produced no final frame (rc={rc}): {stderr.strip()}")
            if not final.get("ok", True):
                raise ToolExecutionError(final.get("error", "worker reported failure"))
            return final.get("output")
        finally:
            watcher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watcher
            if call_id is not None:
                self.procs.pop(call_id, None)

    async def _watch_cancel(self, token: CancelToken, proc) -> None:
        """Kill the worker as soon as the token is tripped (true cancellation)."""
        try:
            await token.wait()
            if proc.returncode is None:
                proc.kill()
        except asyncio.CancelledError:
            return

    async def kill(self, call_id: str) -> None:
        proc = self.procs.pop(call_id, None)
        if proc is None or proc.returncode is not None:
            return
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        with contextlib.suppress(Exception):
            await proc.wait()


class Sandbox:
    """Backend selector façade (§23).

    ``run`` wraps the backend call in the token context so a pre-cancelled call
    never starts; ``kill`` fans out to every backend so the one that owns the
    live resource performs the real teardown.
    """

    def __init__(self, backends: Optional[dict] = None, *, default_backend: str = "inproc",
                 thread_workers: int = 4) -> None:
        self.backends: dict[str, BaseBackend] = dict(backends or {})
        self.backends.setdefault("inproc", InProcessBackend())
        self.backends.setdefault("thread", ThreadBackend(max_workers=thread_workers))
        self.backends.setdefault("subproc", SubprocessBackend())
        self.default_backend = default_backend

    def backend_for(self, spec) -> BaseBackend:
        name = spec.backend or self.default_backend
        try:
            return self.backends[name]
        except KeyError as exc:
            raise SandboxError(f"no backend registered for {name!r}") from exc

    async def run(self, spec, args: dict, token: CancelToken,
                  progress: Optional[Callable[[dict], Any]] = None, *,
                  call_id: Optional[str] = None) -> Any:
        """Invoke ``spec`` through its backend, honouring the cancel token."""
        reporter = progress or _noop_progress
        backend = self.backend_for(spec)
        async with token:
            token.raise_if_cancelled()
            return await backend.invoke(spec, args, token, reporter, call_id=call_id)

    async def kill(self, call_id: str) -> None:
        """Real teardown of the resource owned by ``call_id``, if any (§16.3)."""
        for backend in self.backends.values():
            await backend.kill(call_id)

