"""Out-of-process tool worker (§23, L2).

Invoked as ``python -m linteum_agent.runtime.tools.worker`` with one JSON request on stdin::

    {"entrypoint": "mypkg.mytools:my_fn", "args": {...}}

and answering with newline-delimited JSON frames on stdout::

    {"kind": "progress", "data": {"chunk": "..."}}
    {"kind": "final", "ok": true, "output": ...}
    {"kind": "final", "ok": false, "code": "E_TOOL_ERROR", "error": "..."}

Keeping the protocol this small means a tool written in any language can join
the party by speaking the same three frame shapes — the parent
(:class:`linteum_agent.runtime.tools.sandbox.SubprocessBackend`) already treats the function as
"somewhere else".  ``progress`` frames are byte-batched (4 KB) so a chatty tool
cannot flood the bus one line at a time (Checklist #9).
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import json
import sys

PROGRESS_BATCH_BYTES = 4096


class _Done:
    """A trivially-awaitable so ``progress`` works for sync *and* async tools."""

    __slots__ = ()

    def __await__(self):
        return iter(())


def _emit(frame: dict) -> None:
    sys.stdout.write(json.dumps(frame, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _safe(value):
    """Coerce a tool result to something JSON-serialisable (fallback: ``str``)."""
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        return str(value)


def _resolve(entrypoint: str):
    module_name, _, attr = (entrypoint or "").partition(":")
    if not module_name:
        raise ValueError(f"invalid entrypoint: {entrypoint!r}")
    obj = importlib.import_module(module_name)
    for part in (p for p in attr.split(".") if p):
        obj = getattr(obj, part)
    return obj


def _accepts_progress(fn) -> bool:
    try:
        return "progress" in inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False


class _Reporter:
    """Batches ``chunk`` progress into ≤4 KB frames; flushes on non-chunk data."""

    def __init__(self, batch_bytes: int = PROGRESS_BATCH_BYTES) -> None:
        self._batch_bytes = batch_bytes
        self._chunks: list[str] = []
        self._size = 0

    def __call__(self, payload) -> _Done:
        data = dict(payload) if isinstance(payload, dict) else {"chunk": str(payload)}
        chunk = data.get("chunk")
        if isinstance(chunk, str):
            self._chunks.append(chunk)
            self._size += len(chunk.encode("utf-8"))
            if self._size >= self._batch_bytes:
                self.flush()
        else:
            self.flush()
            _emit({"kind": "progress", "data": data})
        return _Done()

    def flush(self) -> None:
        if self._chunks:
            _emit({"kind": "progress", "data": {"chunk": "".join(self._chunks)}})
            self._chunks = []
            self._size = 0


async def _drain(result):
    """Await a coroutine, or stream an async generator into progress frames."""
    if inspect.isasyncgen(result):
        reporter = _Reporter()
        collected: list = []
        async for item in result:
            collected.append(item)
            reporter(item if isinstance(item, dict) else {"chunk": str(item)})
        reporter.flush()
        if collected and all(isinstance(c, str) for c in collected):
            return "".join(collected)
        return collected
    return await result


def main(argv=None) -> int:
    argv = sys.argv if argv is None else argv
    reporter = _Reporter()
    try:
        request = json.loads(sys.stdin.read() or "{}")
        fn = _resolve(request.get("entrypoint", ""))
        kwargs = dict(request.get("args") or {})
        if _accepts_progress(fn):
            kwargs["progress"] = reporter
        result = fn(**kwargs)
        if inspect.isawaitable(result) or inspect.isasyncgen(result):
            result = asyncio.run(_drain(result))
        reporter.flush()
        _emit({"kind": "final", "ok": True, "output": _safe(result)})
        return 0
    except Exception as exc:  # noqa: BLE001 - turns into a failed tool.call event
        reporter.flush()
        _emit({
            "kind": "final",
            "ok": False,
            "code": "E_TOOL_ERROR",
            "error": f"{type(exc).__name__}: {exc}",
        })
        return 0


if __name__ == "__main__":  # pragma: no cover - process entrypoint
    raise SystemExit(main())
