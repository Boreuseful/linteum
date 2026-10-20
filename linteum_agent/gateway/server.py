"""GatewayServer: connection lifecycle, whitelist and backpressure (§4.2 / §6.3).

The server is deliberately thin.  It owns:

* one :class:`ClientConnection` per transport (bounded outbound queue + writer
  task), so a slow WebSocket can never block the bus;
* command decoding, size limits, hello/version negotiation and audit records;
* the session → :class:`~linteum_agent.gateway.session.SessionGateway` registry
  (one pump per live session);
* session catalogue commands (create/list/get/delete).

It does **not** interpret business events — that is the projector's job — and it
never lets a client publish an arbitrary event: only :data:`KNOWN_COMMANDS` are
routed, and each maps to one controlled publish in ``SessionGateway``.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import time
from typing import Optional

from ..config.options import SessionOptions
from ..config.schema import AppConfig, ConfigError, GatewaySettings
from ..config.service import ConfigService, EffectiveConfig
from ..runtime.host import AgentHost, SessionLocked, SessionNotFound
from .errors import (
    E_INTERNAL,
    E_PAYLOAD_TOO_LARGE,
    E_PROTOCOL,
    E_PROTOCOL_VERSION,
    E_SESSION_LOCKED,
    E_SESSION_NOT_FOUND,
    E_SESSION_WORKSPACE,
    E_UNKNOWN_COMMAND,
    GatewayError,
    LOCATION_SERVER,
)
from .log import get_logger
from .protocol import (
    FEATURES,
    KNOWN_COMMANDS,
    PROTOCOL_VERSION,
    Command,
    CommandResult,
    Frame,
    command_session_id,
    decode,
    encode,
    error_frame,
    hello_payload,
    validate_command,
)
from .session import SessionGateway
from .transports.base import Transport

#: Frames that remain deliverable while a session subscription is paused after
#: an overflow (they are control/command replies, not the dropped stream).
_PAUSED_ALLOWED = frozenset({
    "gap", "snapshot", "error", "bye", "flow", "history.page", "artifact.chunk",
})

#: A replay stream blocks when the outbound queue reaches this much headroom.
#: Without the pause the whole history is enqueued in one synchronous turn, the
#: writer task never runs, and the queue overflows into a ``gap``.
REPLAY_HEADROOM_FRAMES = 64
REPLAY_HEADROOM_BYTES = 64 * 1024
REPLAY_POLL_SECONDS = 0.001
#: A client that stops reading must not pin the session's command lock forever:
#: after this long without headroom the replay falls back to the explicit gap.
REPLAY_DRAIN_TIMEOUT = 30.0

logger = get_logger("server")


class ClientConnection:
    """One client's bounded outbound queue plus its writer task.

    All bus-facing calls are synchronous ``put_nowait`` operations.  The writer
    task is the only component that awaits the transport, which keeps the bus
    handler watchdog far away from network I/O (§6.3 / §7.7).
    """

    def __init__(self, connection_id: int, transport: Transport,
                 limits, *, max_frame_bytes: Optional[int] = None) -> None:
        self.id = connection_id
        self.transport = transport
        self.limits = limits
        self.max_frame_bytes = int(
            max_frame_bytes or getattr(limits, "max_frame_bytes", 1_048_576)
        )
        self.hello_done = False
        self.client_info: dict = {}

        self._queue: asyncio.Queue = asyncio.Queue()
        self._queued_bytes = 0
        self._writer: Optional[asyncio.Task] = None
        self._writing = False
        self._closed = False
        self._paused: set[str] = set()
        self._last_durable: dict[str, int] = {}
        self._dropped_ephemeral = 0
        self._dropped_durable = 0
        self._last_flow = 0.0

    # -- lifecycle ----------------------------------------------------------
    def start(self) -> None:
        if self._writer is None:
            self._writer = asyncio.create_task(self._write_loop())

    async def close(self, *, flush: bool = True, timeout: float = 2.0) -> None:
        if self._closed:
            return
        if flush and self._writer is not None:
            await self._wait_drained(timeout)
        self._closed = True
        writer, self._writer = self._writer, None
        if writer is not None:
            writer.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await writer
        with contextlib.suppress(Exception):
            await self.transport.close()

    async def _wait_drained(self, timeout: float) -> None:
        """Let the writer flush acknowledgements before EOF tears it down."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.0, timeout)
        writer = self._writer
        while (
            (not self._queue.empty() or self._writing)
            and loop.time() < deadline
            and writer is not None
            and not writer.done()
        ):
            await asyncio.sleep(0.005)
        await asyncio.sleep(0)  # give the writer one last scheduling turn

    # -- sending ------------------------------------------------------------
    def send_frame(self, frame: Frame) -> bool:
        if self._closed:
            return False
        session_id = frame.session_id
        if session_id in self._paused and frame.frame not in _PAUSED_ALLOWED:
            return False
        text = encode(frame)
        size = len(text.encode("utf-8"))
        if size > self.max_frame_bytes:
            if frame.ephemeral:
                self._dropped_ephemeral += 1
                logger.debug(
                    "connection %d dropped oversized ephemeral %s frame "
                    "(%d > %d bytes)",
                    self.id, frame.frame, size, self.max_frame_bytes,
                )
                self._maybe_flow()
                return False
            self._dropped_durable += 1
            logger.warning(
                "connection %d replaced oversized durable %s frame "
                "(%d > %d bytes) with a notice",
                self.id, frame.frame, size, self.max_frame_bytes,
            )
            frame = Frame.create(
                "notice",
                {"kind": "frame_too_large", "frame": frame.frame, "size": size},
                session_id=session_id,
                seq=frame.seq,
            )
            text = encode(frame)
            size = len(text.encode("utf-8"))
        if frame.ephemeral and self._over_capacity(size):
            self._dropped_ephemeral += 1
            logger.debug(
                "connection %d dropped ephemeral %s frame (queue full: %d frames, "
                "%d bytes)",
                self.id, frame.frame, self._queue.qsize(), self._queued_bytes,
            )
            self._maybe_flow()
            return False
        if self._over_capacity(size):
            return self._overflow(frame, text)
        self._queue.put_nowait((text, frame))
        self._queued_bytes += size
        return True

    async def send_replay_frame(self, frame: Frame) -> bool:
        """Enqueue one replayed frame, giving the writer room to drain first.

        Replay loops have no other await points, so the writer task cannot run
        and a long history fills the bounded queue in a single turn.  Waiting
        for headroom keeps the replay streaming instead of being truncated by
        :meth:`_overflow`.
        """
        if not await self._wait_for_replay_room():
            # The client stopped reading.  Take the normal overflow path now
            # instead of waiting out another timeout on every frame.
            return self._overflow(frame, "")
        return self.send_frame(frame)

    async def _wait_for_replay_room(self) -> bool:
        """Wait until the writer has made room; ``False`` means it timed out."""
        frames = int(getattr(self.limits, "client_queue_frames", 1_024))
        max_bytes = int(getattr(self.limits, "client_queue_bytes", 1_048_576))
        # Leave room for control frames and the command result that follow the
        # replay; never reserve more than half the configured queue.
        frame_mark = max(1, frames - min(REPLAY_HEADROOM_FRAMES, frames // 2))
        byte_mark = max(1, max_bytes - min(REPLAY_HEADROOM_BYTES, max_bytes // 2))
        loop = asyncio.get_running_loop()
        deadline = loop.time() + REPLAY_DRAIN_TIMEOUT
        while not self._closed and (
            self._queue.qsize() >= frame_mark or self._queued_bytes >= byte_mark
        ):
            if loop.time() >= deadline:
                return False
            await asyncio.sleep(REPLAY_POLL_SECONDS)
        return True

    def send_result(self, result: CommandResult) -> bool:
        if self._closed:
            return False
        text = encode(result)
        size = len(text.encode("utf-8"))
        if size > self.max_frame_bytes:
            result = CommandResult.failure(result.reply_to, GatewayError(
                E_PAYLOAD_TOO_LARGE, "command result exceeds max_frame_bytes",
                location=LOCATION_SERVER,
            ))
            text = encode(result)
            size = len(text.encode("utf-8"))
        if self._over_capacity(size):
            self._drop_ephemeral()
        if self._over_capacity(size):
            # Results are command acknowledgements; dropping them would hang the
            # client.  Make room by dropping the oldest frames (never results),
            # and tell each affected session that it must resubscribe.
            dropped = self._drop_frames_until(size)
            self._emit_gaps(dropped)
        self._queue.put_nowait((text, None))
        self._queued_bytes += size
        return True

    # -- queue management ---------------------------------------------------
    def _over_capacity(self, extra: int = 0) -> bool:
        frames = int(getattr(self.limits, "client_queue_frames", 1_024))
        max_bytes = int(getattr(self.limits, "client_queue_bytes", 1_048_576))
        return (
            self._queue.qsize() + (1 if extra else 0) > frames
            or self._queued_bytes + extra > max_bytes
        )

    def _drain(self) -> list:
        items = []
        while not self._queue.empty():
            items.append(self._queue.get_nowait())
        self._queued_bytes = 0
        return items

    def _requeue(self, items: list) -> None:
        for item in items:
            self._queue.put_nowait(item)
            self._queued_bytes += len(item[0].encode("utf-8"))

    def _drop_ephemeral(self) -> int:
        items = self._drain()
        kept = [item for item in items if item[1] is None or not item[1].ephemeral]
        self._dropped_ephemeral += len(items) - len(kept)
        self._requeue(kept)
        return len(items) - len(kept)

    def _drop_frames_until(self, extra: int) -> list:
        """Drop oldest non-result frames to make room; returns dropped frames."""
        dropped: list = []
        while self._over_capacity(extra):
            items = self._drain()
            if not items:
                break
            index = next((i for i, item in enumerate(items) if item[1] is not None), None)
            if index is None:  # only results left; nothing safe to drop
                self._requeue(items)
                break
            _, frame = items.pop(index)
            dropped.append(frame)
            self._dropped_durable += 1
            self._requeue(items)
        return dropped

    def _emit_gaps(self, dropped: list) -> None:
        """Tell every session whose queued frames were dropped to resubscribe."""
        seen: set[str] = set()
        for frame in dropped:
            session_id = frame.session_id
            if not session_id or session_id in seen:
                continue
            seen.add(session_id)
            self._paused.add(session_id)
            last = self._last_durable.get(session_id, 0)
            to_seq = int(frame.seq) if frame.seq is not None else last
            gap = Frame.create(
                "gap",
                {"from_seq": last + 1, "to_seq": to_seq},
                session_id=session_id,
                seq=to_seq,
            )
            gap_text = encode(gap)
            self._queue.put_nowait((gap_text, gap))
            self._queued_bytes += len(gap_text.encode("utf-8"))

    def _overflow(self, frame: Frame, text: str) -> bool:
        """Durable overflow: drop this session's queued frames, send ``gap``."""
        session_id = frame.session_id
        if session_id is None:
            self._dropped_durable += 1
            logger.warning(
                "connection %d dropped durable %s frame without a session",
                self.id, frame.frame,
            )
            return False
        logger.warning(
            "connection %d outbound queue overflow for session %s; pausing the "
            "subscription and sending a gap",
            self.id, session_id,
        )
        self._paused.add(session_id)
        items = self._drain()
        kept = []
        for item in items:
            queued = item[1]
            if queued is not None and queued.session_id == session_id:
                self._dropped_durable += 1
                continue
            kept.append(item)
        self._requeue(kept)

        last = self._last_durable.get(session_id, 0)
        to_seq = int(frame.seq) if frame.seq is not None else max(last, 0)
        gap = Frame.create(
            "gap",
            {"from_seq": last + 1, "to_seq": to_seq},
            session_id=session_id,
            seq=to_seq,
        )
        gap_text = encode(gap)
        self._emit_gaps(self._drop_frames_until(len(gap_text.encode("utf-8"))))
        self._queue.put_nowait((gap_text, gap))
        self._queued_bytes += len(gap_text.encode("utf-8"))
        return False

    def _maybe_flow(self) -> None:
        now = time.monotonic()
        if now - self._last_flow < 1.0 or self._closed:
            return
        self._last_flow = now
        frame = Frame.create(
            "flow",
            {"dropped_ephemeral": self._dropped_ephemeral, "throttled": True},
            ephemeral=True,
        )
        text = encode(frame)
        if not self._over_capacity(len(text.encode("utf-8"))):
            self._queue.put_nowait((text, frame))
            self._queued_bytes += len(text.encode("utf-8"))

    # -- session pause ------------------------------------------------------
    def pause_session(self, session_id: str) -> None:
        self._paused.add(session_id)

    def resume_session(self, session_id: str) -> None:
        self._paused.discard(session_id)

    def last_durable_seq(self, session_id: str) -> int:
        return int(self._last_durable.get(session_id, 0))

    # -- writer -------------------------------------------------------------
    async def _write_loop(self) -> None:
        while True:
            text, frame = await self._queue.get()
            self._queued_bytes = max(0, self._queued_bytes - len(text.encode("utf-8")))
            self._writing = True
            try:
                await self.transport.send(text)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - a broken socket closes the connection
                logger.debug(
                    "connection %d writer failed: %s: %s",
                    self.id, type(exc).__name__, exc,
                )
                self._closed = True
                with contextlib.suppress(Exception):
                    await self.transport.close()  # unblock the server's ``recv``
                return
            finally:
                self._writing = False
            if frame is not None and not frame.ephemeral and frame.session_id and frame.seq:
                current = self._last_durable.get(frame.session_id, 0)
                if frame.seq > current:
                    self._last_durable[frame.session_id] = int(frame.seq)

    # -- introspection ------------------------------------------------------
    @property
    def stats(self) -> dict:
        return {
            "queued": self._queue.qsize(),
            "queued_bytes": self._queued_bytes,
            "dropped_ephemeral": self._dropped_ephemeral,
            "dropped_durable": self._dropped_durable,
        }


class GatewayServer:
    """Route commands to the catalogue or to one session's gateway."""

    def __init__(
        self,
        host: AgentHost,
        config: Optional[GatewaySettings | EffectiveConfig | AppConfig] = None,
        *,
        config_service: Optional[ConfigService] = None,
        gap_timeout: float = 0.020,
        server_name: str = "linteum",
    ) -> None:
        self.host = host
        self.config_service = config_service or host.config_service
        self._fixed_settings = _gateway_settings(config)
        self.gap_timeout = gap_timeout
        self.server_name = server_name

        self._gateways: dict[str, SessionGateway] = {}
        self._gateway_locks: dict[str, asyncio.Lock] = {}
        self._gateway_lock = asyncio.Lock()
        self._connections: dict[int, ClientConnection] = {}
        self._connection_ids = itertools.count(1)
        self._closed = False
        self._audit_sink = None

    # -- configuration ------------------------------------------------------
    def _settings(self) -> GatewaySettings:
        if self._fixed_settings is not None:
            return self._fixed_settings
        cfg = self._current_config()
        return cfg.gateway

    def _current_config(self) -> AppConfig:
        if self.config_service is not None:
            return self.config_service.effective(strict=False).config
        return self.host.current_config()

    # -- lifecycle ----------------------------------------------------------
    async def serve(self, transport: Transport) -> ClientConnection:
        """Run the command loop for one transport until EOF/close."""
        settings = self._settings()
        connection = ClientConnection(
            next(self._connection_ids), transport, settings.limits,
            max_frame_bytes=settings.limits.max_frame_bytes,
        )
        connection.start()
        self._connections[connection.id] = connection
        logger.info(
            "connection %d opened (%d live)", connection.id, len(self._connections)
        )
        try:
            while not self._closed:
                raw = await transport.recv()
                if raw is None:
                    break
                await self._handle_raw(connection, raw, settings)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - one connection must not kill the server
            logger.exception("connection %d failed", connection.id)
            self._audit({
                "kind": "gateway_connection_error",
                "level": "error",
                "msg": f"{type(exc).__name__}: {exc}",
                "ts": time.time(),
                "client_id": connection.id,
            })
        finally:
            await self._drop_connection(connection)
            logger.info(
                "connection %d closed (%d live, client=%s)",
                connection.id, len(self._connections),
                connection.client_info.get("id"),
            )
        return connection

    async def close(self, *, close_host: bool = False) -> None:
        if self._closed:
            return
        self._closed = True
        logger.info(
            "gateway server closing (%d session(s), %d connection(s), close_host=%s)",
            len(self._gateways), len(self._connections), close_host,
        )
        for gateway in list(self._gateways.values()):
            await gateway.close()
        self._gateways.clear()
        for connection in list(self._connections.values()):
            connection.send_frame(Frame.create(
                "bye", {"reason": "server shutdown"}, ephemeral=True,
            ))
            await connection.close()
        self._connections.clear()
        if close_host:
            await self.host.close()

    # -- command handling ---------------------------------------------------
    async def _handle_raw(self, connection: ClientConnection, raw,
                          settings: GatewaySettings) -> None:
        try:
            envelope = decode(raw, max_bytes=settings.limits.max_command_bytes)
        except GatewayError as exc:
            logger.debug("connection %d sent an invalid envelope: %s",
                         connection.id, exc.code)
            connection.send_frame(error_frame(exc))
            return
        if envelope.get("kind") != "cmd":
            connection.send_frame(error_frame(GatewayError(
                E_PROTOCOL, "envelope kind must be 'cmd'", location=LOCATION_SERVER,
            )))
            return

        command: Optional[Command] = None
        request_id = envelope.get("id") if isinstance(envelope.get("id"), str) else None
        started = time.monotonic()
        try:
            command = Command.from_dict(envelope)
            if not connection.hello_done and command.name != "hello":
                raise GatewayError(
                    E_PROTOCOL, "hello must be the first command",
                    location=LOCATION_SERVER,
                )
            if command.name != "hello" and command.v != PROTOCOL_VERSION:
                raise GatewayError(
                    E_PROTOCOL_VERSION,
                    f"unsupported protocol version {command.v}; server speaks {PROTOCOL_VERSION}",
                    location=LOCATION_SERVER,
                )
            validate_command(command)
            result = await self._dispatch(connection, command, settings)
            connection.send_result(CommandResult.success(command.id, result))
            self._audit_command(connection, command, "ok", started)
        except GatewayError as exc:
            if command is not None:
                connection.send_result(CommandResult.failure(command.id, exc))
            elif request_id:
                connection.send_result(CommandResult.failure(request_id, exc))
            else:
                connection.send_frame(error_frame(exc))
            self._audit_command(connection, command, "error", started, code=exc.code)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - convert to structured data (P4)
            logger.exception(
                "command %s crashed (client=%d)",
                command.name if command is not None else request_id,
                connection.id,
            )
            error = GatewayError(
                E_INTERNAL, f"{type(exc).__name__}: {exc}",
                location=LOCATION_SERVER, exc=exc,
            )
            if command is not None:
                connection.send_result(CommandResult.failure(command.id, error))
            elif request_id:
                connection.send_result(CommandResult.failure(request_id, error))
            else:
                connection.send_frame(error_frame(error))
            self._audit_command(connection, command, "error", started, code=error.code)

    async def _dispatch(self, connection: ClientConnection, command: Command,
                        settings: GatewaySettings) -> dict:
        if command.name == "hello":
            return self._hello(connection, command, settings)
        if command.name == "ping":
            return {"pong": True, "ts": time.time()}
        if command.name == "session.create":
            return await self._session_create(connection, command)
        if command.name == "session.list":
            return await self._session_list(command)
        if command.name == "session.delete":
            return await self._session_delete(command)
        if command.name == "session.get":
            session_id = command_session_id(command)
            gateway = await self._gateway_for_readable(session_id)
            info = await self.host.get_session_info(session_id)
            if info is None:
                raise GatewayError(
                    E_SESSION_NOT_FOUND, f"session not found: {session_id!r}",
                    location=LOCATION_SERVER,
                )
            return {"session": info.to_dict(), "snapshot": gateway.snapshot()}
        if command.name == "session.unsubscribe":
            session_id = command_session_id(command)
            gateway = self._gateways.get(session_id)
            if gateway is None:
                return {"unsubscribed": False}
            return await gateway.dispatch(command, connection)
        if command.name in KNOWN_COMMANDS:
            session_id = command_session_id(command)
            if command.name in ("session.subscribe", "history.read", "artifact.read"):
                gateway = await self._gateway_for_readable(session_id)
            else:
                gateway = await self._gateway_for(session_id)
            return await gateway.dispatch(command, connection)
        raise GatewayError(
            E_UNKNOWN_COMMAND, f"unknown command: {command.name!r}",
            location=LOCATION_SERVER,
        )

    def _hello(self, connection: ClientConnection, command: Command,
               settings: GatewaySettings) -> dict:
        offered = command.args.get("protocol", PROTOCOL_VERSION)
        versions = offered if isinstance(offered, list) else [offered]
        if PROTOCOL_VERSION not in versions:
            raise GatewayError(
                E_PROTOCOL_VERSION,
                f"no common protocol version (client {versions}, server {PROTOCOL_VERSION})",
                location=LOCATION_SERVER,
            )
        connection.hello_done = True
        connection.client_info = dict(command.args.get("client") or {})
        logger.info(
            "connection %d hello from %s (protocol=%s)",
            connection.id, connection.client_info.get("id"), PROTOCOL_VERSION,
        )
        return hello_payload(
            server=self.server_name,
            limits={
                "client_queue_frames": settings.limits.client_queue_frames,
                "client_queue_bytes": settings.limits.client_queue_bytes,
                "max_command_bytes": settings.limits.max_command_bytes,
                "max_frame_bytes": settings.limits.max_frame_bytes,
                "history_page_size": settings.history.page_size,
            },
            features=FEATURES,
        )

    # -- session catalogue --------------------------------------------------
    async def _session_create(self, connection: ClientConnection,
                              command: Command) -> dict:
        args = command.args
        try:
            options = SessionOptions(
                workspace=args["workspace"],
                session_id=args.get("session_id"),
                profile=args.get("profile") or self._current_config().agent.default_profile,
                title=args.get("title"),
                metadata=dict(args.get("metadata") or {}),
            )
            handle = await self.host.open_session(
                options, profile=args.get("profile")
            )
            logger.info(
                "session %s created (workspace=%s, profile=%s, client=%d)",
                handle.session_id, options.workspace, options.profile,
                connection.id,
            )
        except SessionLocked as exc:
            raise GatewayError(
                E_SESSION_LOCKED, str(exc), retryable=True,
                location=LOCATION_SERVER, context={"owner": exc.owner},
            ) from exc
        except ConfigError as exc:
            code = E_SESSION_WORKSPACE if exc.code == "E_SESSION_WORKSPACE" else exc.code
            raise GatewayError(
                code, str(exc), location=LOCATION_SERVER,
                context={"key": exc.key},
            ) from exc
        gateway = await self._gateway_for(handle.session_id)
        info = await self.host.get_session_info(handle.session_id)
        return {
            "session": info.to_dict() if info is not None else {
                "session_id": handle.session_id,
            },
            "snapshot": gateway.snapshot(),
            "warnings": list(handle.warnings),
        }

    async def _session_list(self, command: Command) -> dict:
        limit = int(command.args.get("limit") or 50)
        offset = int(command.args.get("offset") or 0)
        workspace = command.args.get("workspace")
        infos = await self.host.list_sessions(
            limit=limit, offset=offset, workspace=workspace
        )
        return {
            "sessions": [info.to_dict() for info in infos],
            "limit": limit,
            "offset": offset,
        }

    async def _session_delete(self, command: Command) -> dict:
        session_id = command_session_id(command)
        # Taking the live gateway also takes the lease; a session owned by
        # another process must not be deleted out from under it.
        await self._gateway_for(session_id)
        gateway = self._gateways.pop(session_id, None)
        if gateway is not None:
            gateway.broadcast("bye", {"reason": "session deleted"})
            await gateway.close()
        info = await self.host.get_session_info(session_id)
        if info is None:
            raise GatewayError(
                E_SESSION_NOT_FOUND, f"session not found: {session_id!r}",
                location=LOCATION_SERVER,
            )
        await self.host.delete_session(session_id)
        logger.info("session %s deleted", session_id)
        return {"deleted": True, "session_id": session_id}

    # -- session gateways ---------------------------------------------------
    async def _gateway_for(self, session_id: str, *,
                           read_only: bool = False) -> SessionGateway:
        gateway = self._gateways.get(session_id)
        if gateway is not None and (read_only or not gateway.read_only):
            return gateway
        lock = self._gateway_locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            gateway = self._gateways.get(session_id)
            if gateway is not None and (read_only or not gateway.read_only):
                return gateway
            if read_only:
                info = await self.host.get_session_info(session_id)
                if info is None:
                    raise GatewayError(
                        E_SESSION_NOT_FOUND, f"session not found: {session_id!r}",
                        location=LOCATION_SERVER,
                    )
            else:
                try:
                    await self.host.ensure_session(session_id)
                except SessionLocked as exc:
                    raise GatewayError(
                        E_SESSION_LOCKED, str(exc), retryable=True,
                        location=LOCATION_SERVER, context={"owner": exc.owner},
                    ) from exc
                except SessionNotFound as exc:
                    raise GatewayError(
                        E_SESSION_NOT_FOUND, str(exc), location=LOCATION_SERVER,
                    ) from exc
                except ConfigError as exc:
                    code = exc.code if exc.code.startswith("E_") else E_SESSION_WORKSPACE
                    raise GatewayError(
                        code, str(exc), location=LOCATION_SERVER,
                        context={"key": exc.key},
                    ) from exc
            cfg = self._current_config()
            session_gateway = SessionGateway(
                self.host,
                session_id,
                limits=cfg.gateway.limits,
                history=cfg.gateway.history,
                input_policy=cfg.agent.session.input_policy,
                gap_timeout=self.gap_timeout,
                read_only=read_only,
            )
            await session_gateway.start()
            if gateway is not None:  # replace a stale read-only view
                logger.debug("session %s replacing stale read-only gateway",
                             session_id)
                await gateway.close()
            self._gateways[session_id] = session_gateway
            logger.info("session %s gateway opened (read_only=%s)",
                        session_id, read_only)
            return session_gateway

    async def _gateway_for_readable(self, session_id: str) -> SessionGateway:
        """Live gateway when this process owns the session, else read-only."""
        try:
            return await self._gateway_for(session_id)
        except GatewayError as exc:
            if exc.code != E_SESSION_LOCKED:
                raise
            return await self._gateway_for(session_id, read_only=True)

    async def _drop_connection(self, connection: ClientConnection) -> None:
        self._connections.pop(connection.id, None)
        for gateway in list(self._gateways.values()):
            gateway.drop_subscriber(connection.id)
        await connection.close()

    # -- audit --------------------------------------------------------------
    def set_audit_sink(self, sink) -> None:
        """Register a structured audit sink (never raises into the gateway)."""
        self._audit_sink = sink

    def _audit_command(self, connection: ClientConnection, command: Optional[Command],
                       status: str, started: float, *, code: Optional[str] = None) -> None:
        elapsed_ms = (time.monotonic() - started) * 1000.0
        if status == "ok":
            logger.debug(
                "cmd %s ok (client=%d session=%s %.1fms)",
                command.name if command else "?", connection.id,
                command.session_id if command else None, elapsed_ms,
            )
        else:
            logger.info(
                "cmd %s failed [%s] (client=%d session=%s %.1fms)",
                command.name if command else "?", code, connection.id,
                command.session_id if command else None, elapsed_ms,
            )
        record = {
            "kind": "gateway_command",
            "level": "info" if status == "ok" else "warning",
            "msg": (
                f"cmd {command.name if command else '?'} {status}"
                + (f" ({code})" if code else "")
            ),
            "ts": time.time(),
            "client_id": connection.id,
            "session_id": command.session_id if command else None,
            "command": command.name if command else None,
            "status": status,
            "code": code,
            "duration_ms": round((time.monotonic() - started) * 1000.0, 3),
        }
        self._audit(record)

    def _audit(self, record: dict) -> None:
        sink = self._audit_sink
        if sink is not None:
            try:
                sink(record)
            except Exception:  # noqa: BLE001 - audit must never break the gateway
                return
        emit = getattr(self.host.bus, "emit_log", None)
        if emit is not None:
            emit(record)

    # -- introspection ------------------------------------------------------
    @property
    def settings(self) -> GatewaySettings:
        """Current gateway settings (queue/limits/WebSocket/backpressure)."""
        return self._settings()

    @property
    def connections(self) -> int:
        return len(self._connections)

    @property
    def sessions(self) -> tuple:
        return tuple(self._gateways)

    def metrics(self) -> dict:
        return {
            "connections": len(self._connections),
            "sessions": len(self._gateways),
            "client_queues": {
                connection.id: connection.stats
                for connection in self._connections.values()
            },
        }


def _gateway_settings(config) -> Optional[GatewaySettings]:
    if config is None:
        return None
    if isinstance(config, GatewaySettings):
        return config
    if isinstance(config, EffectiveConfig):
        return config.config.gateway
    if isinstance(config, AppConfig):
        return config.gateway
    raise TypeError(f"unsupported gateway config: {type(config).__name__}")


__all__ = ["ClientConnection", "GatewayServer"]
