"""Newline-delimited JSON over stdio (§4.4 Served (stdio)).

One JSON envelope per line on stdin/stdout.  The transport is intentionally
boring: it knows nothing about sessions and does not batch, retry or interpret
messages.  Parents that spawn ``linteum serve --stdio`` get process isolation
and a trivial debugging format (``tail -f`` works).

Logging must go to **stderr**; stdout is reserved for protocol frames.

``open()`` prefers the asyncio pipe transport (zero threads) but falls back to
small threaded adapters when stdin/stdout is a regular file or a terminal that
the selector transport refuses.  This matters for shells that redirect stdio to
a file — the protocol still works, it is just not selector-driven.
"""

from __future__ import annotations

import asyncio
import sys
import threading
from typing import Optional

#: Default maximum line size (one command/frame).  The gateway enforces its own
#: smaller ``max_command_bytes``/``max_frame_bytes`` on top of this.
DEFAULT_MAX_LINE_BYTES = 4 * 1024 * 1024

#: Internal queue marker for an oversized line.
_TOO_LONG = object()


class _ThreadedReader:
    """``readline`` adapter for streams the selector loop cannot register."""

    def __init__(self, stream, max_line_bytes: int) -> None:
        self._stream = stream
        self._max = max_line_bytes
        self._queue: asyncio.Queue = asyncio.Queue()
        self._task = asyncio.create_task(self._loop())

    async def _loop(self) -> None:
        try:
            while True:
                line = await asyncio.to_thread(self._stream.readline)
                if not line:
                    self._queue.put_nowait(None)
                    return
                if len(line) > self._max:
                    self._queue.put_nowait(_TOO_LONG)
                    return
                self._queue.put_nowait(line)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - surface EOF instead of hanging
            self._queue.put_nowait(None)

    async def readline(self):
        item = await self._queue.get()
        if item is _TOO_LONG:
            raise ConnectionError(f"stdio line exceeds {self._max} bytes")
        return item

    def close(self) -> None:
        self._task.cancel()


class _ThreadedWriter:
    """Buffered writer that performs the blocking write off-loop."""

    def __init__(self, stream) -> None:
        self._stream = stream
        self._buffer = bytearray()
        self._lock = threading.Lock()

    def write(self, data: bytes) -> None:
        self._buffer.extend(data)

    async def drain(self) -> None:
        if not self._buffer:
            return
        data = bytes(self._buffer)
        self._buffer.clear()
        await asyncio.to_thread(self._write_sync, data)

    def _write_sync(self, data: bytes) -> None:
        with self._lock:
            self._stream.write(data.decode("utf-8"))
            self._stream.flush()

    def close(self) -> None:
        return None

    async def wait_closed(self) -> None:
        return None


class StdioTransport:
    """One JSONL connection over stdin/stdout (or an injected reader/writer)."""

    def __init__(self, reader, writer, *,
                 max_line_bytes: int = DEFAULT_MAX_LINE_BYTES) -> None:
        self._reader = reader
        self._writer = writer
        self.max_line_bytes = max_line_bytes
        self._closed = False

    @classmethod
    async def open(
        cls,
        *,
        stdin=None,
        stdout=None,
        max_line_bytes: int = DEFAULT_MAX_LINE_BYTES,
    ) -> "StdioTransport":
        """Attach to the current process's stdin/stdout (or supplied streams)."""
        loop = asyncio.get_running_loop()
        stdin = stdin or sys.stdin
        stdout = stdout or sys.stdout

        try:
            reader = asyncio.StreamReader(limit=max_line_bytes, loop=loop)
            protocol = asyncio.StreamReaderProtocol(reader)
            await loop.connect_read_pipe(lambda: protocol, stdin)
        except (ValueError, OSError, NotImplementedError):
            reader = _ThreadedReader(stdin, max_line_bytes)

        try:
            write_transport, write_protocol = await loop.connect_write_pipe(
                asyncio.streams.FlowControlMixin, stdout
            )
            writer = asyncio.StreamWriter(write_transport, write_protocol, reader, loop)
        except (ValueError, OSError, NotImplementedError):
            writer = _ThreadedWriter(stdout)

        return cls(reader, writer, max_line_bytes=max_line_bytes)

    async def recv(self) -> Optional[str]:
        try:
            line = await self._reader.readline()
        except (ValueError, asyncio.LimitOverrunError) as exc:
            raise ConnectionError(f"stdio line exceeds {self.max_line_bytes} bytes") from exc
        if not line:  # EOF
            return None
        if len(line) > self.max_line_bytes:
            raise ConnectionError(f"stdio line exceeds {self.max_line_bytes} bytes")
        try:
            return line.rstrip(b"\r\n").decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ConnectionError("stdio line is not valid UTF-8") from exc

    async def send(self, data: str | bytes) -> None:
        if self._closed:
            raise ConnectionError("stdio transport is closed")
        if isinstance(data, str):
            data = data.encode("utf-8")
        self._writer.write(data + b"\n")
        await self._writer.drain()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        close_reader = getattr(self._reader, "close", None)
        if close_reader is not None:
            close_reader()
        self._writer.close()
        try:
            await self._writer.wait_closed()
        except Exception:  # noqa: BLE001 - closing an already-broken pipe is fine
            pass


__all__ = ["DEFAULT_MAX_LINE_BYTES", "StdioTransport"]
