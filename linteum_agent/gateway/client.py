"""Transport-agnostic gateway client SDK (§4.2 / §8).

``AgentClient`` speaks protocol v1 over any :class:`~linteum_agent.gateway.transports.base.Transport`:
in-process queues, stdio JSONL and WebSocket all use the same object.  It
imports **no runtime code**, so the same protocol can be reimplemented in
TypeScript or another language without copying framework internals.

The client keeps two pieces of local state:

* ``_last_seq[session_id]`` — the last *durable* frame received; this is the
  reconnect cursor and is never advanced by ephemeral deltas;
* a queue of frames for the renderer, so command results (which resolve a
  future) and asynchronous frames never interleave incorrectly.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import uuid
from typing import AsyncIterator, Iterable, Mapping, Optional

from .protocol import (
    PROTOCOL_VERSION,
    Command,
    CommandResult,
    Frame,
    decode,
    encode,
)
from .transports.base import Transport

#: Default command timeout (seconds).  Commands acknowledge persistence, not
#: completion, so this only covers transport/gateway work.
DEFAULT_COMMAND_TIMEOUT = 30.0

#: Queue sentinel: the reader has stopped; ``next_frame`` turns it into a clear
#: ``ConnectionClosed`` instead of leaking ``None`` to renderers.
_EOF = object()


class ClientError(Exception):
    """A command failed; ``error_info`` is the structured gateway payload."""

    def __init__(self, error_info: Mapping) -> None:
        self.error_info = dict(error_info)
        self.code = str(self.error_info.get("code", "E_INTERNAL"))
        super().__init__(str(self.error_info.get("message") or self.code))


class ConnectionClosed(ClientError):
    def __init__(self, message: str = "gateway connection closed") -> None:
        super().__init__({
            "code": "E_CONNECTION_CLOSED",
            "source": "client",
            "message": message,
            "retryable": True,
            "enters_history": False,
        })


class AgentClient:
    """One client connection speaking the v1 command/frame protocol."""

    def __init__(
        self,
        transport: Transport,
        *,
        client_id: Optional[str] = None,
        client_info: Optional[Mapping] = None,
        features: Iterable[str] = (),
        frame_queue_size: int = 0,
    ) -> None:
        self.transport = transport
        self.client_id = client_id or f"client-{uuid.uuid4().hex[:12]}"
        self.client_info = dict(client_info or {})
        self.features = tuple(features)
        self.hello_info: Optional[dict] = None

        self._ids = itertools.count(1)
        self._pending: dict[str, asyncio.Future] = {}
        self._frames: asyncio.Queue = asyncio.Queue(maxsize=frame_queue_size)
        self._last_seq: dict[str, int] = {}
        self._reader: Optional[asyncio.Task] = None
        self._closed = asyncio.Event()
        self._close_error: Optional[BaseException] = None

    # -- lifecycle ----------------------------------------------------------
    async def connect(self) -> dict:
        """Send ``hello`` and negotiate the protocol version.

        Returns the ``hello`` result payload (protocol/features/limits).
        """
        if self._reader is None:
            self._reader = asyncio.create_task(self._read_loop())
        result = await self.command(
            "hello",
            {
                "protocol": PROTOCOL_VERSION,
                "client": {"id": self.client_id, **self.client_info},
                "features": list(self.features),
            },
        )
        self.hello_info = dict(result)
        return result

    async def close(self) -> None:
        if self._closed.is_set():
            return
        self._closed.set()
        reader, self._reader = self._reader, None
        if reader is not None:
            reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reader
        with contextlib.suppress(Exception):
            await self.transport.close()
        self._fail_pending(ConnectionClosed("client closed"))

    @property
    def closed(self) -> bool:
        return self._closed.is_set()

    # -- commands -----------------------------------------------------------
    def _next_id(self) -> str:
        return f"c-{next(self._ids)}"

    async def command(
        self,
        name: str,
        args: Optional[Mapping] = None,
        *,
        session_id: Optional[str] = None,
        trace_id: Optional[str] = None,
        timeout: Optional[float] = DEFAULT_COMMAND_TIMEOUT,
    ) -> dict:
        """Send one whitelisted command and await its result payload."""
        if self._closed.is_set():
            raise self._close_error or ConnectionClosed()
        cmd_id = self._next_id()
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[cmd_id] = future
        command = Command(
            id=cmd_id,
            name=name,
            args=dict(args or {}),
            session_id=session_id,
            trace_id=trace_id,
        )
        try:
            await self.transport.send(encode(command))
            if timeout is None:
                return await future
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending.pop(cmd_id, None)
            raise ClientError({
                "code": "E_REQUEST_TIMEOUT",
                "source": "client",
                "message": f"command {name!r} timed out after {timeout}s",
                "retryable": True,
                "enters_history": False,
            }) from None
        except BaseException:
            self._pending.pop(cmd_id, None)
            raise

    # -- subscriptions ------------------------------------------------------
    async def subscribe(
        self,
        session_id: str,
        *,
        after_seq: int = 0,
        mode: str = "view",
        frames: Optional[Iterable[str]] = None,
    ) -> dict:
        return await self.command(
            "session.subscribe",
            {
                "session_id": session_id,
                "after_seq": int(after_seq),
                "mode": mode,
                "frames": list(frames or ()),
            },
            session_id=session_id,
        )

    async def unsubscribe(self, session_id: str) -> dict:
        return await self.command(
            "session.unsubscribe", {"session_id": session_id}, session_id=session_id
        )

    def last_seq(self, session_id: str) -> int:
        """Last durable seq received for ``session_id`` (the resume cursor)."""
        return int(self._last_seq.get(session_id, 0))

    async def resubscribe(
        self, session_id: str, *, mode: str = "view",
        frames: Optional[Iterable[str]] = None,
    ) -> dict:
        """Reconnect helper: subscribe from the last durable frame seen."""
        return await self.subscribe(
            session_id, after_seq=self.last_seq(session_id), mode=mode, frames=frames
        )

    # -- frame consumption --------------------------------------------------
    async def next_frame(self, *, timeout: Optional[float] = None) -> Frame:
        if timeout is None:
            item = await self._frames.get()
        else:
            item = await asyncio.wait_for(self._frames.get(), timeout=timeout)
        if item is _EOF:
            self._frames.put_nowait(_EOF)  # keep the stream terminated for others
            raise self._close_error or ConnectionClosed()
        return item

    async def iter_frames(self) -> AsyncIterator[Frame]:
        while True:
            try:
                yield await self.next_frame()
            except ConnectionClosed:
                return
            except asyncio.CancelledError:
                raise

    @property
    def pending_frames(self) -> int:
        return self._frames.qsize()

    # -- convenience wrappers ----------------------------------------------
    async def create_session(
        self,
        workspace: str,
        *,
        session_id: Optional[str] = None,
        profile: Optional[str] = None,
        title: Optional[str] = None,
        metadata: Optional[Mapping] = None,
    ) -> dict:
        args = {"workspace": workspace}
        if session_id is not None:
            args["session_id"] = session_id
        if profile is not None:
            args["profile"] = profile
        if title is not None:
            args["title"] = title
        if metadata is not None:
            args["metadata"] = dict(metadata)
        return await self.command("session.create", args)

    async def list_sessions(self, *, limit: int = 50, offset: int = 0,
                            workspace: Optional[str] = None) -> dict:
        args: dict = {"limit": limit, "offset": offset}
        if workspace is not None:
            args["workspace"] = workspace
        return await self.command("session.list", args)

    async def get_session(self, session_id: str) -> dict:
        return await self.command(
            "session.get", {"session_id": session_id}, session_id=session_id
        )

    async def delete_session(self, session_id: str) -> dict:
        return await self.command(
            "session.delete", {"session_id": session_id}, session_id=session_id
        )

    async def send_message(
        self,
        session_id: str,
        text: str,
        *,
        client_message_id: Optional[str] = None,
        metadata: Optional[Mapping] = None,
        trace_id: Optional[str] = None,
    ) -> dict:
        client_message_id = client_message_id or f"m-{uuid.uuid4().hex[:16]}"
        args = {"text": text, "client_message_id": client_message_id}
        if metadata is not None:
            args["metadata"] = dict(metadata)
        return await self.command(
            "user.message", args, session_id=session_id, trace_id=trace_id
        )

    async def interrupt(self, session_id: str, *, reason: Optional[str] = None) -> dict:
        args = {"reason": reason} if reason else {}
        return await self.command("user.interrupt", args, session_id=session_id)

    async def decide_approval(
        self, session_id: str, call_id: str, *, decision: str,
        reason: Optional[str] = None,
    ) -> dict:
        args = {"call_id": call_id, "decision": decision}
        if reason is not None:
            args["reason"] = reason
        return await self.command("approval.decide", args, session_id=session_id)

    async def resume_agent(self, session_id: str) -> dict:
        return await self.command("agent.resume", {}, session_id=session_id)

    async def history(
        self, session_id: str, *, after_seq: int = 0, limit: int = 100,
        mode: str = "view",
    ) -> dict:
        return await self.command(
            "history.read",
            {"after_seq": after_seq, "limit": limit, "mode": mode},
            session_id=session_id,
        )

    async def read_artifact(
        self, session_id: str, ref: str, *, offset: int = 0,
        limit: int = 65536,
    ) -> dict:
        return await self.command(
            "artifact.read",
            {"ref": ref, "offset": offset, "limit": limit},
            session_id=session_id,
        )

    async def ping(self) -> dict:
        return await self.command("ping", {})

    # -- reader -------------------------------------------------------------
    async def _read_loop(self) -> None:
        try:
            while not self._closed.is_set():
                raw = await self.transport.recv()
                if raw is None:
                    raise ConnectionClosed()
                envelope = decode(raw)
                kind = envelope.get("kind")
                if kind == "result":
                    self._resolve(CommandResult.from_dict(envelope))
                elif kind == "frame":
                    self._receive_frame(Frame.from_dict(envelope))
                # Unknown kinds are ignored for forward compatibility (§5.5).
        except asyncio.CancelledError:
            raise
        except BaseException as exc:  # noqa: BLE001 - surface to every waiter
            self._close_error = exc
        finally:
            self._closed.set()
            self._fail_pending(self._close_error or ConnectionClosed())
            with contextlib.suppress(asyncio.QueueFull):
                self._frames.put_nowait(_EOF)  # sentinel: ``iter_frames`` stops

    def _resolve(self, result: CommandResult) -> None:
        future = self._pending.pop(result.reply_to, None)
        if future is None or future.done():
            return
        if not result.ok:
            future.set_exception(ClientError(result.error or {
                "code": "E_INTERNAL", "source": "gateway",
                "message": "command failed without error_info",
                "enters_history": False,
            }))
            return
        future.set_result(result.result)

    def _receive_frame(self, frame: Frame) -> None:
        if not frame.ephemeral and frame.seq is not None and frame.session_id:
            current = self._last_seq.get(frame.session_id, 0)
            if frame.seq > current:
                self._last_seq[frame.session_id] = frame.seq
        self._frames.put_nowait(frame)

    def _fail_pending(self, exc: BaseException) -> None:
        for future in list(self._pending.values()):
            if not future.done():
                future.set_exception(exc)
        self._pending.clear()


async def open_client(transport: Transport, **kwargs) -> AgentClient:
    """Create, connect and return a client (convenience for embedded hosts)."""
    client = AgentClient(transport, **kwargs)
    try:
        await client.connect()
    except BaseException:
        await client.close()
        raise
    return client


__all__ = [
    "AgentClient",
    "ClientError",
    "ConnectionClosed",
    "DEFAULT_COMMAND_TIMEOUT",
    "open_client",
]
