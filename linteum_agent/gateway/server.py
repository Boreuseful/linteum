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
import copy
import itertools
import json
import time
import uuid
from dataclasses import fields
from typing import Any, Mapping, Optional

from ..config.meta import (
    MAX_LIST_ITEMS,
    FieldSpec,
    field_specs,
    get_spec,
    is_writable_spec,
)
from ..config.options import SessionOptions
from ..config.schema import AppConfig, ConfigError, GatewaySettings, LLMConfig, app_to_dict
from ..config.service import ConfigService, EffectiveConfig, env_var_for
from ..runtime.host import AgentHost, SessionLocked, SessionNotFound
from ..runtime.plugins.errors import PluginError, PluginManifestError, PluginNotFound
from .config_scope import runtime_scope
from .errors import (
    E_CONFIG_COMPOSITE_UNSUPPORTED,
    E_CONFIG_LOCAL_ONLY,
    E_CONFIG_MANAGE_DISABLED,
    E_CONFIG_PROFILE_EXISTS,
    E_CONFIG_PROFILE_INCOMPLETE,
    E_CONFIG_PROFILE_IN_USE,
    E_CONFIG_PROFILE_NAME,
    E_CONFIG_PROFILE_NOT_FOUND,
    E_CONFIG_PROFILE_RESERVED,
    E_CONFIG_RANGE,
    E_CONFIG_READONLY,
    E_CONFIG_UNAVAILABLE,
    E_INTERNAL,
    E_PAYLOAD_TOO_LARGE,
    E_PLUGIN_CONFIG,
    E_PLUGIN_MANAGE_DISABLED,
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

#: Fixed client-facing messages for config failures.  ``ConfigError`` messages
#: can embed the offending value (e.g. an API key pasted into an enum field) or
#: YAML source, so the original exception is never forwarded as ``detail``.
_CONFIG_SAFE_MESSAGES = {
    "E_CONFIG_UNKNOWN": "unknown configuration key",
    "E_CONFIG_TYPE": "invalid configuration value",
    "E_CONFIG_RANGE": "configuration value is out of range",
    "E_CONFIG_ENUM": "configuration value is not allowed",
    "E_CONFIG_JSON": "configuration value must be valid JSON",
    "E_CONFIG_KEY_OVERLAP": "configuration keys overlap",
    "E_CONFIG_COMPOSITE_UNSUPPORTED": "composite configuration keys are not editable",
    "E_CONFIG_CONFLICT": "configuration changed since it was read",
    "E_CONFIG_PARSE": "configuration file is not valid YAML",
    "E_CONFIG_IO": "configuration file is not readable",
    "E_CONFIG_PROVIDER_CONFLICT": "provider settings conflict with another profile",
    "E_CONFIG_PROVIDER_PARAMS": "provider request defaults differ between profiles",
    "E_CONFIG_PROFILE_NOT_FOUND": "configured profile does not exist",
    "E_CONFIG_FORCE_BACKGROUND_ALLOW": "force_background requires agent.background.allow",
    "E_CONFIG_PROFILE_IN_USE": "profile is still referenced",
    "E_CONFIG_PROFILE_EXISTS": "profile already exists",
    "E_CONFIG_PROFILE_INCOMPLETE": "profile is missing required fields",
    "E_CONFIG_PROFILE_RESERVED": "profile name is reserved",
    "E_CONFIG_PROFILE_NAME": "invalid profile name",
}

#: Profile names that must never be created (they collide with the secret
#: masking of ``agent.profiles`` leaves or with ``agent.llm`` itself).
_RESERVED_PROFILE_NAMES = frozenset({"api_key", "password", "secret", "token"})

#: Upper bound for one scalar-list write (web-config Phase 3 §6.3.3).
CONFIG_MAX_LIST_ITEMS = MAX_LIST_ITEMS

#: Editable leaves of a named LLM profile (``LLMConfig`` fields).
_LLM_FIELDS = frozenset(f.name for f in fields(LLMConfig))


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
            ), name=result.name)
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
        instance_id: Optional[str] = None,
    ) -> None:
        self.host = host
        self.config_service = config_service or host.config_service
        self._fixed_settings = _gateway_settings(config)
        self.gap_timeout = gap_timeout
        self.server_name = server_name
        # Stable per-process identity, reported by hello so a client can tell
        # a restarted gateway from a reconnected one (§5.6).
        self._instance_id = instance_id or uuid.uuid4().hex
        # §11.5.4: capture at startup (apply_scope=restart), never per command.
        self._manage_enabled = self._initial_manage_enabled()
        # web-config §6.1: same startup-capture model for configuration writes.
        self._config_manage_enabled = self._initial_config_manage_enabled()
        self._config_lock = asyncio.Lock()

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

    def _initial_manage_enabled(self) -> bool:
        """``gateway.plugins.manage`` as seen when this server started."""
        if self._fixed_settings is not None:
            return bool(self._fixed_settings.plugins.manage)
        try:
            return bool(self._current_config().gateway.plugins.manage)
        except Exception:  # noqa: BLE001 - an invalid config is already fatal elsewhere
            return False

    def _initial_config_manage_enabled(self) -> bool:
        """``gateway.config.manage`` as seen when this server started."""
        if self._fixed_settings is not None:
            return bool(self._fixed_settings.config.manage)
        try:
            return bool(self._current_config().gateway.config.manage)
        except Exception:  # noqa: BLE001 - an invalid config is already fatal elsewhere
            return False

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
            connection.send_result(
                CommandResult.success(command.id, result, name=command.name)
            )
            self._audit_command(connection, command, "ok", started)
        except GatewayError as exc:
            if command is not None:
                connection.send_result(
                    CommandResult.failure(command.id, exc, name=command.name)
                )
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
                connection.send_result(
                    CommandResult.failure(command.id, error, name=command.name)
                )
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
        if command.name == "mode.list":
            return await self._mode_list()
        if command.name == "plugin.list":
            return await self._plugin_list()
        if command.name == "plugin.enable":
            return await self._plugin_set_enabled(command, enable=True)
        if command.name == "plugin.disable":
            return await self._plugin_set_enabled(command, enable=False)
        if command.name == "plugin.set_allowed":
            self._require_plugin_runtime(connection)
            return await self._plugin_set_allowed(command)
        if command.name == "plugin.load":
            self._require_plugin_runtime(connection)
            return await self._plugin_load(command)
        if command.name == "plugin.unload":
            self._require_plugin_runtime(connection)
            return await self._plugin_unload(command)
        if command.name == "plugin.reload":
            self._require_plugin_runtime(connection)
            return await self._plugin_reload(command)
        if command.name == "config.get":
            return await self._config_get(connection, command)
        if command.name == "config.set":
            return await self._config_set(connection, command)
        if command.name == "config.unset":
            return await self._config_unset(connection, command)
        if command.name == "config.profile.set":
            return await self._config_profile_set(connection, command)
        if command.name == "config.profile.delete":
            return await self._config_profile_delete(connection, command)
        if command.name == "config.profile.duplicate":
            return await self._config_profile_duplicate(connection, command)
        if command.name == "config.fallback.set":
            return await self._config_fallback_set(connection, command)
        if command.name == "config.fallback.delete":
            return await self._config_fallback_delete(connection, command)
        if command.name == "config.fallback.move":
            return await self._config_fallback_move(connection, command)
        if command.name == "session.list":
            return await self._session_list(command)
        if command.name == "session.children":
            return await self._session_children(command)
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
            if command.name in (
                "session.subscribe", "history.read", "artifact.read",
                "session.context", "approval.decide", "session.tasks",
            ):
                # A subagent is only reachable through the read-only follow
                # gateway (which permits ``approval.decide``); a root keeps the
                # live-owner / read-only fallback semantics.
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
            instance_id=self._instance_id,
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
                mode=args.get("mode"),
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

    async def _mode_list(self) -> dict:
        catalog = await self.host.mode_catalog()
        modes = await self.host.list_modes()
        return {
            "modes": [
                {
                    "id": mode.id,
                    "name": mode.name,
                    "description": mode.description,
                    "source": mode.source_id,
                    "builtin": mode.builtin,
                    "default": mode.default,
                }
                for mode in modes
            ],
            "invalid": [
                {
                    **issue.to_dict(),
                    "id": f"{issue.source_id}/{issue.name}",
                }
                for issue in catalog.invalid
            ],
            "warnings": list(catalog.warnings),
        }

    def _plugin_allow_env_override(self) -> bool:
        """True when ``agent.plugins.allow`` comes from an environment override."""
        if self.config_service is None:
            return False
        try:
            effective = self.config_service.effective(strict=False)
        except ConfigError:
            return False
        return effective.sources.get("agent.plugins.allow") == "env"

    def _plugin_file_config(self) -> Optional[AppConfig]:
        """The validated file-level config, or ``None`` when unreadable/missing."""
        if self.config_service is None:
            return None
        try:
            return self.config_service.store.load(create=False)
        except ConfigError:
            return None

    def _plugin_manage_state(self) -> tuple[bool, Optional[AppConfig], list[str]]:
        """(manage, file config, warnings) for ``plugin.list`` (§11.2)."""
        warnings: list[str] = []
        if self.config_service is None:
            return False, None, warnings
        env_override = self._plugin_allow_env_override()
        file_config = self._plugin_file_config()
        if file_config is None:
            warnings.append("plugin.config_unreadable")
            return False, None, warnings
        if env_override:
            warnings.append("plugin.allow_env_override")
        return self._manage_enabled and not env_override, file_config, warnings

    async def _plugin_list(self) -> dict:
        summaries = await self.host.list_plugins()
        manage, file_config, config_warnings = self._plugin_manage_state()
        plugins = []
        invalid = []
        for summary in summaries:
            if summary.state == "invalid":
                error = summary.error or {}
                invalid.append({
                    "source": summary.source,
                    "code": error.get("code", "E_PLUGIN_MANIFEST"),
                    "error": error.get("message", ""),
                })
                continue
            enabled = (
                summary.id in file_config.agent.plugins.allow
                if file_config is not None
                else summary.state != "disabled"
            )
            entry = {
                "id": summary.id,
                "version": summary.version,
                "state": summary.state,
                "enabled": enabled,
                "trust": summary.trust,
                "description": summary.description,
                "source": summary.source,
                "tools": list(summary.tools),
                "skills": list(summary.skills),
                "modes": list(summary.modes),
                "digest": summary.digest,
            }
            # §4.5 v0.4: sources and secret names only (never values).  Only
            # active records carry them; unloaded/disabled/failed are empty.
            if summary.config:
                entry["config"] = dict(summary.config)
            if summary.secrets:
                entry["secrets"] = list(summary.secrets)
            if summary.hooks:
                entry["hooks"] = list(summary.hooks)
            if summary.subscribes:
                entry["subscribes"] = list(summary.subscribes)
            if summary.publishes:
                entry["publishes"] = list(summary.publishes)
            # §11.2 v0.2: explain failed/disabled plugins without a new command.
            if summary.error:
                entry["error"] = dict(summary.error)
            if summary.warnings:
                entry["warnings"] = list(summary.warnings)
            plugins.append(entry)
        result = {
            "plugins": plugins,
            "invalid": invalid,
            "warnings": [*self.host.plugin_warnings(), *config_warnings],
            "manage": manage,
        }
        # Post-v0.4 apply extension: expose the file-level allow so a local
        # client can submit exactly the saved list back with mode="apply".
        # The field is omitted when the file config is unavailable.
        if file_config is not None:
            result["allow"] = list(file_config.agent.plugins.allow)
        return result

    # -- plugin management gates (§11.5.4 / §5.6) --------------------------
    def _require_manage_enabled(self, *, hint: str = "") -> None:
        """Raise unless ``gateway.plugins.manage`` is enabled.

        The flag is captured when the server starts (apply_scope=restart);
        ``hint`` is an optional client-facing remedy kept per gate.
        """
        if self._manage_enabled:
            return
        context = {"hint": hint} if hint else {}
        raise GatewayError(
            E_PLUGIN_MANAGE_DISABLED,
            "plugin management is disabled",
            location=LOCATION_SERVER,
            context=context,
        )

    def _require_config_service(self) -> ConfigService:
        """Return the :class:`ConfigService`, or raise ``E_PLUGIN_CONFIG``."""
        service = self.config_service
        if service is None:
            raise GatewayError(
                E_PLUGIN_CONFIG,
                "configuration service is not available",
                location=LOCATION_SERVER,
            )
        return service

    def _require_allow_not_env_overridden(self) -> None:
        """Raise ``E_PLUGIN_CONFIG`` when ``agent.plugins.allow`` comes from env."""
        if self._plugin_allow_env_override():
            raise GatewayError(
                E_PLUGIN_CONFIG,
                "agent.plugins.allow is overridden by the environment",
                location=LOCATION_SERVER,
                context={
                    "hint": "unset the environment variable and restart the gateway",
                },
            )

    def _require_config_readable(self, service: ConfigService) -> AppConfig:
        """Load the config file; a read/parse failure is ``E_PLUGIN_CONFIG``."""
        try:
            return service.store.load(create=False)
        except ConfigError as exc:
            raise GatewayError(
                E_PLUGIN_CONFIG,
                "cannot read the configuration file",
                location=LOCATION_SERVER,
                context={"code": exc.code},
                exc=exc,
            ) from exc

    async def _plugin_set_enabled(self, command: Command, *,
                                  enable: bool) -> dict:
        """Persistently add/remove one id from ``agent.plugins.allow`` (§11.5)."""
        self._require_manage_enabled()
        service = self._require_config_service()
        self._require_allow_not_env_overridden()
        file_config = self._require_config_readable(service)

        plugin_id = str(command.args.get("id"))
        summaries = await self.host.list_plugins()
        summary = next(
            (item for item in summaries if item.id == plugin_id), None,
        )
        if summary is None:
            raise GatewayError(
                PluginNotFound.code,
                f"plugin not found: {plugin_id!r}",
                location=LOCATION_SERVER,
            )
        if summary.state == "invalid":
            error = summary.error or {}
            raise GatewayError(
                PluginManifestError.code,
                error.get("message")
                or f"invalid plugin manifest for {plugin_id!r}",
                location=LOCATION_SERVER,
            )

        allow = list(file_config.agent.plugins.allow)
        changed = False
        if enable and plugin_id not in allow:
            allow.append(plugin_id)
            changed = True
        elif not enable and plugin_id in allow:
            allow.remove(plugin_id)
            changed = True
        if changed:
            try:
                service.set(
                    "agent.plugins.allow", allow,
                    expected_revision=service.revision(),
                )
            except ConfigError as exc:
                raise GatewayError(
                    E_PLUGIN_CONFIG,
                    "cannot update agent.plugins.allow",
                    location=LOCATION_SERVER,
                    context={"code": exc.code},
                    exc=exc,
                ) from exc

        restart_required = (
            summary.state in {"disabled", "discovered", "unloaded"}
            if enable
            else summary.state in {"active", "loading", "quiescing"}
        )
        plugin = {
            "id": summary.id,
            "version": summary.version,
            "state": summary.state,
            "enabled": enable,
            "trust": summary.trust,
            "description": summary.description,
            "source": summary.source,
            "tools": list(summary.tools),
            "skills": list(summary.skills),
            "modes": list(summary.modes),
            "digest": summary.digest,
        }
        self.host._log({
            "kind": "plugin",
            "level": "info",
            "msg": f"plugin {'enable' if enable else 'disable'} {plugin_id}",
            "ts": time.time(),
            "action": "enable" if enable else "disable",
            "id": plugin_id,
            "changed": changed,
            "restart_required": restart_required,
        })
        return {
            "plugin": plugin,
            "changed": changed,
            "persisted": True,
            "restart_required": restart_required,
            "warnings": [],
        }

    # -- config management (web-config §6) ---------------------------------
    def _require_config_read(self, connection: ClientConnection) -> ConfigService:
        """``config.get`` is local-or-managed; secrets stay masked either way."""
        if not self._config_manage_enabled and not getattr(
            connection.transport, "is_local", True
        ):
            raise GatewayError(
                E_CONFIG_MANAGE_DISABLED,
                "configuration is not readable from a remote connection",
                location=LOCATION_SERVER,
                context={
                    "hint": "enable gateway.config.manage and restart the gateway",
                },
            )
        return self._config_service_or_error()

    def _require_config_write(self, connection: ClientConnection) -> ConfigService:
        """Writes need ``gateway.config.manage`` **and** a local transport."""
        if not self._config_manage_enabled:
            raise GatewayError(
                E_CONFIG_MANAGE_DISABLED,
                "configuration management is disabled",
                location=LOCATION_SERVER,
                context={
                    "hint": "enable gateway.config.manage and restart the gateway",
                },
            )
        if not getattr(connection.transport, "is_local", True):
            raise GatewayError(
                E_CONFIG_LOCAL_ONLY,
                "configuration writes require a local connection",
                location=LOCATION_SERVER,
                context={
                    "hint": "only embedded, stdio or loopback clients may write",
                },
            )
        return self._config_service_or_error()

    def _config_service_or_error(self) -> ConfigService:
        service = self.config_service
        if service is None:
            raise GatewayError(
                E_CONFIG_UNAVAILABLE,
                "configuration service is not available",
                location=LOCATION_SERVER,
            )
        return service

    @staticmethod
    def _config_gateway_error(
        exc: ConfigError, service: ConfigService,
    ) -> GatewayError:
        """Map a config failure without echoing values or YAML source."""
        context: dict = {"config_code": exc.code}
        if exc.key:
            context["key"] = exc.key
        if exc.hint:
            context["hint"] = exc.hint
        if exc.code == "E_CONFIG_CONFLICT":
            context["current_revision"] = service.revision()
        if exc.code in ("E_CONFIG_PARSE", "E_CONFIG_IO"):
            context["path"] = service.path()
        return GatewayError(
            exc.code,
            _CONFIG_SAFE_MESSAGES.get(exc.code, "configuration error"),
            location=LOCATION_SERVER,
            context=context,
        )

    @staticmethod
    def _config_specs(effective: EffectiveConfig,
                      sections: Optional[list[str]]) -> list[FieldSpec]:
        specs = field_specs(effective.config)
        if not sections:
            return specs
        wanted = set(sections)
        return [
            spec for spec in specs
            if spec.section in wanted
            or any(spec.key == value or spec.key.startswith(value + ".")
                   for value in wanted)
        ]

    async def _config_get(self, connection: ClientConnection,
                          command: Command) -> dict:
        self._require_config_read(connection)
        service = self._config_service_or_error()
        sections = command.args.get("sections")
        include_schema = command.args.get("include_schema")
        if include_schema is None:
            include_schema = True
        include_fields = command.args.get("include_fields")
        if include_fields is None:
            include_fields = True

        def _read() -> dict:
            effective = service.effective(strict=False)
            specs = self._config_specs(effective, sections)
            values = effective.values() if include_fields else {}
            schema = service.schema() if include_schema else None
            if schema is not None and sections:
                wanted = set(sections)
                schema["sections"] = [
                    section for section in schema["sections"]
                    if section["key"] in wanted
                    or any(section["key"].startswith(value + ".")
                           for value in wanted)
                ]
            fields: dict = {}
            if include_fields:
                for spec in specs:
                    value = dict(values[spec.key])
                    if value.get("source") == "env":
                        value["env_var"] = env_var_for(spec.key, service.prefix)
                    fields[spec.key] = value
            return {
                "revision": effective.revision,
                "path": service.path(),
                "exists": service.exists(),
                "manage": self._config_manage_enabled,
                "writable": (
                    self._config_manage_enabled
                    and getattr(connection.transport, "is_local", True)
                ),
                "schema": schema,
                "fields": fields,
                "runtime_scopes": (
                    {spec.key: runtime_scope(spec.key) for spec in specs}
                    if include_fields else {}
                ),
                "issues": [issue.to_dict() for issue in effective.issues],
            }

        try:
            return await asyncio.to_thread(_read)
        except ConfigError as exc:
            raise self._config_gateway_error(exc, service) from exc

    @staticmethod
    def _config_check_list_limits(changes: Mapping[str, Any]) -> None:
        """Reject oversized scalar-list writes before touching the file."""
        for key, value in changes.items():
            spec = get_spec(key)
            if spec.type != "list":
                continue
            items = value
            if isinstance(value, str):
                try:
                    items = json.loads(value)
                except (TypeError, ValueError):
                    continue  # coerce_value will produce E_CONFIG_JSON
            if (isinstance(items, (list, tuple))
                    and len(items) > CONFIG_MAX_LIST_ITEMS):
                raise GatewayError(
                    E_CONFIG_RANGE,
                    "configuration list is too long",
                    location=LOCATION_SERVER,
                    context={"key": key, "maximum": CONFIG_MAX_LIST_ITEMS},
                )

    def _config_prepare(self, service: ConfigService,
                        keys) -> None:
        """Reject env-owned and composite keys before touching the file."""
        effective = service.effective(strict=False)
        for key in keys:
            if effective.source(key) == "env":
                raise GatewayError(
                    E_CONFIG_READONLY,
                    f"{key} is overridden by the environment",
                    location=LOCATION_SERVER,
                    context={
                        "key": key,
                        "env_var": env_var_for(key, service.prefix),
                        "hint": "unset the environment variable and restart",
                    },
                )
            spec = get_spec(key)
            if key == "agent.plugins.allow":
                raise GatewayError(
                    E_CONFIG_COMPOSITE_UNSUPPORTED,
                    "agent.plugins.allow is managed by the plugin runtime",
                    location=LOCATION_SERVER,
                    context={
                        "key": key,
                        "hint": "use plugin.set_allowed from the Plugins page",
                    },
                )
            if not is_writable_spec(spec):
                raise GatewayError(
                    E_CONFIG_COMPOSITE_UNSUPPORTED,
                    "composite configuration keys are not editable",
                    location=LOCATION_SERVER,
                    context={
                        "key": key,
                        "hint": (
                            "edit this key in YAML or use a structured command"
                        ),
                    },
                )

    @staticmethod
    def _config_result(change_set, service: ConfigService) -> dict:
        apply_result = service.apply(change_set)
        return {
            "revision": change_set.revision,
            "changed": bool(change_set.changes),
            "changes": [change.to_dict() for change in change_set.changes],
            "apply": {
                "declared": {
                    "live": list(apply_result.applied_live),
                    "new_session": list(apply_result.requires_new_session),
                    "restart": list(apply_result.requires_restart),
                },
                "runtime": {
                    change.key: runtime_scope(change.key)
                    for change in change_set.changes
                },
            },
            "warnings": [issue.to_dict() for issue in apply_result.warnings],
        }

    @staticmethod
    def _config_expected_revision(service: ConfigService,
                                  expected: Optional[str]) -> None:
        if expected == "" and service.exists() and service.revision() == "":
            raise GatewayError(
                E_CONFIG_IO,
                "configuration file is not readable",
                location=LOCATION_SERVER,
                context={"path": service.path()},
            )

    async def _config_set(self, connection: ClientConnection,
                          command: Command) -> dict:
        service = self._require_config_write(connection)
        persist = bool(command.args.get("persist", True))
        expected = command.args.get("expected_revision")
        changes = dict(command.args["changes"])

        def _apply() -> dict:
            self._config_expected_revision(service, expected)
            self._config_check_list_limits(changes)
            self._config_prepare(service, changes)
            change_set = service.update(
                changes, expected_revision=expected, persist=persist,
            )
            return self._config_result(change_set, service)

        try:
            async with self._config_lock:
                result = await asyncio.to_thread(_apply)
        except ConfigError as exc:
            raise self._config_gateway_error(exc, service) from exc
        self._config_audit(connection, result, persist)
        return result

    async def _config_unset(self, connection: ClientConnection,
                            command: Command) -> dict:
        service = self._require_config_write(connection)
        expected = command.args["expected_revision"]
        keys = list(command.args["keys"])

        def _apply() -> dict:
            self._config_expected_revision(service, expected)
            self._config_prepare(service, keys)
            change_set = service.unset_many(
                keys, expected_revision=expected, persist=True,
            )
            return self._config_result(change_set, service)

        try:
            async with self._config_lock:
                result = await asyncio.to_thread(_apply)
        except ConfigError as exc:
            raise self._config_gateway_error(exc, service) from exc
        self._config_audit(connection, result, True)
        return result

    def _config_audit(self, connection: ClientConnection, result: dict,
                      persist: bool, *,
                      action: Optional[str] = None,
                      target: Optional[str] = None,
                      extra: Optional[Mapping[str, Any]] = None) -> None:
        if not persist or not result.get("changed"):
            return
        record = {
            "kind": "config",
            "level": "info",
            "msg": f"config update ({len(result['changes'])} key(s))",
            "ts": time.time(),
            "revision": result["revision"],
            "changed": True,
            "keys": [change["key"] for change in result["changes"]],
            "client_id": connection.id,
            "transport": type(connection.transport).__name__,
        }
        if action:
            record["action"] = action
        if target is not None:
            record["target"] = target
        if extra:
            record.update(extra)
        self.host._log(record)

    # -- named LLM profiles and fallbacks (web-llm-profiles §5.3–§5.6) -----
    @staticmethod
    def _config_file_config(service: ConfigService) -> AppConfig:
        """The validated file-level config (defaults when the file is missing)."""
        return service.store.load(create=False)

    @staticmethod
    def _config_effective(service: ConfigService) -> EffectiveConfig:
        """The merged runtime view, with warnings instead of raised errors."""
        return service.effective(strict=False)

    @staticmethod
    def _config_composite_readonly(service: ConfigService, key: str) -> None:
        """Raise ``E_CONFIG_READONLY`` when a composite key comes from the env."""
        if service.effective(strict=False).source(key) == "env":
            raise GatewayError(
                E_CONFIG_READONLY,
                f"{key} is overridden by the environment",
                location=LOCATION_SERVER,
                context={
                    "key": key,
                    "env_var": env_var_for(key, service.prefix),
                    "hint": "unset the environment variable and restart",
                },
            )

    @staticmethod
    def _merge_llm_values(
        existing: Mapping[str, Any],
        values: Mapping[str, Any],
        *,
        creating: bool,
    ) -> dict:
        """Merge a partial LLM values dict over the raw file values (§5.3).

        Omitting a field keeps the stored value (or ``None`` for a new
        profile); a literal ``null`` clears it.  A new profile must set
        ``provider`` and ``model``.
        """
        unknown = sorted(set(values) - _LLM_FIELDS)
        if unknown:
            raise GatewayError(
                "E_CONFIG_UNKNOWN",
                "unknown configuration value",
                location=LOCATION_SERVER,
                context={"hint": "values must be LLM profile fields"},
            )
        merged = dict(existing)
        merged.update(dict(values))
        if creating and (not merged.get("provider") or not merged.get("model")):
            raise GatewayError(
                E_CONFIG_PROFILE_INCOMPLETE,
                "profile is missing required fields",
                location=LOCATION_SERVER,
                context={"hint": "provider and model are required for a new profile"},
            )
        return merged

    @staticmethod
    def _config_fallback_bounds(length: int, index: int) -> None:
        """Reject a fallback index outside ``[0, len)`` without mutating (§5.6)."""
        if 0 <= index < length:
            return
        raise GatewayError(
            E_CONFIG_RANGE,
            "fallback index is out of range",
            location=LOCATION_SERVER,
            context={
                "key": "agent.fallbacks",
                "index": index,
                "count": length,
            },
        )

    @staticmethod
    def _config_reserved_profile_name(name: str, *, creating: bool) -> None:
        """Reject secret-like names; ``default`` may not name a *new* profile."""
        if name in _RESERVED_PROFILE_NAMES:
            raise GatewayError(
                E_CONFIG_PROFILE_RESERVED,
                "profile name is reserved",
                location=LOCATION_SERVER,
                context={
                    "name": name,
                    "hint": "choose a different profile name",
                },
            )
        if creating and name == "default":
            raise GatewayError(
                E_CONFIG_PROFILE_RESERVED,
                "profile name 'default' is reserved for agent.llm",
                location=LOCATION_SERVER,
                context={
                    "name": name,
                    "hint": "edit agent.llm through config.profile.set {name: default}",
                },
            )

    async def _sessions_using_profile(self, name: str) -> list[str]:
        """Best-effort session catalogue scan for ``metadata.profile`` (§5.4)."""
        matching: list[str] = []
        offset = 0
        limit = 200
        cap = 10_000
        while len(matching) < cap:
            infos = await self.host.list_sessions(
                limit=limit, offset=offset, include_subagents=True,
            )
            if not infos:
                break
            for info in infos:
                if str(info.metadata.get("profile") or "") == name:
                    matching.append(info.session_id)
                if len(matching) >= cap:
                    break
            if len(infos) < limit:
                break
            offset += len(infos)
        return matching

    async def _config_profile_set(self, connection: ClientConnection,
                                  command: Command) -> dict:
        """Create/edit a named LLM profile; ``default`` edits ``agent.llm`` (§5.3)."""
        service = self._require_config_write(connection)
        name = str(command.args["name"])
        values = dict(command.args["values"])
        expected = command.args.get("expected_revision")

        def _apply() -> dict:
            self._config_expected_revision(service, expected)
            file_config = self._config_file_config(service)
            if name == "default":
                updates = {
                    f"agent.llm.{key}": value for key, value in values.items()
                }
                effective = self._config_effective(service)
                for key in updates:
                    if effective.source(key) == "env":
                        raise GatewayError(
                            E_CONFIG_READONLY,
                            f"{key} is overridden by the environment",
                            location=LOCATION_SERVER,
                            context={
                                "key": key,
                                "env_var": env_var_for(key, service.prefix),
                                "hint": "unset the environment variable and restart",
                            },
                        )
                change_set = service.update(
                    updates, expected_revision=expected,
                )
                return self._config_result(change_set, service)
            self._config_composite_readonly(service, "agent.profiles")
            profiles = app_to_dict(file_config)["agent"]["profiles"]
            creating = name not in profiles
            self._config_reserved_profile_name(name, creating=creating)
            merged = self._merge_llm_values(
                profiles.get(name, {}), values, creating=creating,
            )
            profiles[name] = merged
            change_set = service.update(
                {"agent.profiles": profiles}, expected_revision=expected,
            )
            return self._config_result(change_set, service)

        try:
            async with self._config_lock:
                result = await asyncio.to_thread(_apply)
        except ConfigError as exc:
            raise self._config_gateway_error(exc, service) from exc
        self._config_audit(connection, result, True,
                           action="profile.set", target=name)
        return result

    async def _config_profile_delete(self, connection: ClientConnection,
                                     command: Command) -> dict:
        """Delete a named profile behind reference guards (§5.4)."""
        service = self._require_config_write(connection)
        name = str(command.args["name"])
        expected = command.args.get("expected_revision")
        confirm = bool(command.args.get("confirm_in_use", False))
        if name == "default":
            raise GatewayError(
                E_CONFIG_PROFILE_RESERVED,
                "profile name 'default' is reserved for agent.llm",
                location=LOCATION_SERVER,
                context={
                    "name": name,
                    "hint": "edit agent.llm through config.profile.set {name: default}",
                },
            )
        if not confirm:
            sessions = await self._sessions_using_profile(name)
            if sessions:
                raise GatewayError(
                    E_CONFIG_PROFILE_IN_USE,
                    "profile is still referenced by historical sessions",
                    location=LOCATION_SERVER,
                    context={
                        "referenced_by": "sessions",
                        "count": len(sessions),
                        "sample_session_ids": sessions[:5],
                        "hint": (
                            "re-send with confirm_in_use=true to delete anyway; "
                            "those sessions will fail to recover"
                        ),
                    },
                )

        def _apply() -> dict:
            self._config_expected_revision(service, expected)
            self._config_composite_readonly(service, "agent.profiles")
            file_config = self._config_file_config(service)
            profiles = app_to_dict(file_config)["agent"]["profiles"]
            if name not in profiles:
                raise GatewayError(
                    E_CONFIG_PROFILE_NOT_FOUND,
                    "profile does not exist",
                    location=LOCATION_SERVER,
                    context={"name": name},
                )
            effective = self._config_effective(service)
            references = (
                ("agent.default_profile",
                 effective.config.agent.default_profile),
                ("agent.subagents.default_model_profile",
                 effective.config.agent.subagents.default_model_profile),
                ("agent.subagents.allowed_model_profiles",
                 name if name in effective.config.agent.subagents.allowed_model_profiles
                 else None),
            )
            for path, value in references:
                if value == name:
                    raise GatewayError(
                        E_CONFIG_PROFILE_IN_USE,
                        "profile is still referenced by the configuration",
                        location=LOCATION_SERVER,
                        context={
                            "referenced_by": path,
                            "hint": "change the referencing setting before deleting",
                        },
                    )
            profiles.pop(name)
            change_set = service.update(
                {"agent.profiles": profiles}, expected_revision=expected,
            )
            return self._config_result(change_set, service)

        try:
            async with self._config_lock:
                result = await asyncio.to_thread(_apply)
        except ConfigError as exc:
            raise self._config_gateway_error(exc, service) from exc
        self._config_audit(
            connection, result, True, action="profile.delete", target=name,
            extra={"confirmed_in_use": confirm},
        )
        return result

    async def _config_profile_duplicate(self, connection: ClientConnection,
                                        command: Command) -> dict:
        """Copy a profile (or ``agent.llm``) with its raw secret (§5.5)."""
        service = self._require_config_write(connection)
        source_name = str(command.args["from"])
        name = str(command.args["name"])
        expected = command.args.get("expected_revision")

        def _apply() -> dict:
            self._config_expected_revision(service, expected)
            self._config_composite_readonly(service, "agent.profiles")
            file_config = self._config_file_config(service)
            profiles = app_to_dict(file_config)["agent"]["profiles"]
            self._config_reserved_profile_name(name, creating=True)
            if name in profiles:
                raise GatewayError(
                    E_CONFIG_PROFILE_EXISTS,
                    "profile already exists",
                    location=LOCATION_SERVER,
                    context={"name": name},
                )
            if source_name == "default":
                source = app_to_dict(file_config)["agent"]["llm"]
            else:
                source = profiles.get(source_name)
                if source is None:
                    raise GatewayError(
                        E_CONFIG_PROFILE_NOT_FOUND,
                        "profile does not exist",
                        location=LOCATION_SERVER,
                        context={"name": source_name},
                    )
            profiles[name] = copy.deepcopy(source)
            change_set = service.update(
                {"agent.profiles": profiles}, expected_revision=expected,
            )
            return self._config_result(change_set, service)

        try:
            async with self._config_lock:
                result = await asyncio.to_thread(_apply)
        except ConfigError as exc:
            raise self._config_gateway_error(exc, service) from exc
        self._config_audit(connection, result, True,
                           action="profile.duplicate", target=name)
        return result

    async def _config_fallback_set(self, connection: ClientConnection,
                                   command: Command) -> dict:
        """Append (no ``index``) or edit one fallback, keeping raw secrets (§5.6)."""
        service = self._require_config_write(connection)
        values = dict(command.args["values"])
        index = command.args.get("index")
        expected = command.args.get("expected_revision")
        target = "new" if index is None else str(index)

        def _apply() -> dict:
            self._config_expected_revision(service, expected)
            self._config_composite_readonly(service, "agent.fallbacks")
            file_config = self._config_file_config(service)
            fallbacks = app_to_dict(file_config)["agent"]["fallbacks"]
            if index is None:
                fallbacks.append(
                    self._merge_llm_values({}, values, creating=True)
                )
            else:
                self._config_fallback_bounds(len(fallbacks), index)
                fallbacks[index] = self._merge_llm_values(
                    fallbacks[index], values, creating=False,
                )
            change_set = service.update(
                {"agent.fallbacks": fallbacks}, expected_revision=expected,
            )
            return self._config_result(change_set, service)

        try:
            async with self._config_lock:
                result = await asyncio.to_thread(_apply)
        except ConfigError as exc:
            raise self._config_gateway_error(exc, service) from exc
        self._config_audit(connection, result, True,
                           action="fallback.set", target=target)
        return result

    async def _config_fallback_delete(self, connection: ClientConnection,
                                      command: Command) -> dict:
        """Delete one fallback by index (§5.6)."""
        service = self._require_config_write(connection)
        index = int(command.args["index"])
        expected = command.args.get("expected_revision")

        def _apply() -> dict:
            self._config_expected_revision(service, expected)
            self._config_composite_readonly(service, "agent.fallbacks")
            file_config = self._config_file_config(service)
            fallbacks = app_to_dict(file_config)["agent"]["fallbacks"]
            self._config_fallback_bounds(len(fallbacks), index)
            fallbacks.pop(index)
            change_set = service.update(
                {"agent.fallbacks": fallbacks}, expected_revision=expected,
            )
            return self._config_result(change_set, service)

        try:
            async with self._config_lock:
                result = await asyncio.to_thread(_apply)
        except ConfigError as exc:
            raise self._config_gateway_error(exc, service) from exc
        self._config_audit(connection, result, True,
                           action="fallback.delete", target=str(index))
        return result

    async def _config_fallback_move(self, connection: ClientConnection,
                                    command: Command) -> dict:
        """Reorder one fallback; its raw secret travels with the entry (§5.6)."""
        service = self._require_config_write(connection)
        source_index = int(command.args["from"])
        target_index = int(command.args["to"])
        expected = command.args.get("expected_revision")

        def _apply() -> dict:
            self._config_expected_revision(service, expected)
            self._config_composite_readonly(service, "agent.fallbacks")
            file_config = self._config_file_config(service)
            fallbacks = app_to_dict(file_config)["agent"]["fallbacks"]
            self._config_fallback_bounds(len(fallbacks), source_index)
            self._config_fallback_bounds(len(fallbacks), target_index)
            item = fallbacks.pop(source_index)
            fallbacks.insert(target_index, item)
            change_set = service.update(
                {"agent.fallbacks": fallbacks}, expected_revision=expected,
            )
            return self._config_result(change_set, service)

        try:
            async with self._config_lock:
                result = await asyncio.to_thread(_apply)
        except ConfigError as exc:
            raise self._config_gateway_error(exc, service) from exc
        self._config_audit(
            connection, result, True, action="fallback.move",
            target=f"{source_index}->{target_index}",
        )
        return result

    # -- plugin runtime commands (§5.6) -------------------------------------
    def _require_plugin_runtime(self, connection: ClientConnection) -> None:
        """Gate the runtime lifecycle commands (§5.6).

        Checked in spec order: the manage flag, a working configuration
        service with a readable file, no environment override of
        ``agent.plugins.allow``, and a local transport.  Only clients that
        pass all four may touch the running plugin set.  The individual
        checks are shared with :meth:`_plugin_set_enabled`, which checks the
        env override before reading the file.
        """
        self._require_manage_enabled(
            hint=(
                "enable gateway.plugins.manage in the configuration "
                "and restart the gateway"
            ),
        )
        service = self._require_config_service()
        self._require_config_readable(service)
        self._require_allow_not_env_overridden()
        if not getattr(connection.transport, "is_local", True):
            raise GatewayError(
                E_PLUGIN_MANAGE_DISABLED,
                "plugin runtime management requires a local transport",
                location=LOCATION_SERVER,
                context={
                    "hint": (
                        "plugin runtime commands are only available to local "
                        "clients (embedded, stdio or loopback)"
                    ),
                },
            )

    @staticmethod
    def _plugin_gateway_error(exc: PluginError) -> GatewayError:
        """Map a runtime :class:`PluginError` onto a gateway command error."""
        context = {}
        if exc.key:
            context["key"] = exc.key
        if exc.hint:
            context["hint"] = exc.hint
        return GatewayError(
            exc.code, str(exc), retryable=exc.retryable,
            location=LOCATION_SERVER, context=context,
        )

    async def _plugin_set_allowed(self, command: Command) -> dict:
        ids = tuple(command.args["ids"])
        mode = str(command.args.get("mode", "deferred"))
        try:
            result = await self.host.set_plugin_allowed(ids, mode=mode)
        except PluginError as exc:
            raise self._plugin_gateway_error(exc) from exc
        return {
            "allow": list(result.allow),
            "mode": result.mode,
            "changed": result.changed,
            "restart_required": result.restart_required,
            "plugins": [summary.to_dict() for summary in result.summaries],
            "changes": [change.to_dict() for change in result.changes],
        }

    async def _plugin_load(self, command: Command) -> dict:
        plugin_id = str(command.args["id"])
        try:
            summary = await self.host.load_plugin(plugin_id)
        except PluginError as exc:
            raise self._plugin_gateway_error(exc) from exc
        # A validation/setup/commit failure is data, not a command error: the
        # host returns the failed summary and the command acknowledges it.
        return {"plugin": summary.to_dict(), "changed": True}

    async def _plugin_unload(self, command: Command) -> dict:
        plugin_id = str(command.args["id"])
        grace_ms = command.args.get("grace_ms")
        try:
            summary = await self.host.unload_plugin(
                plugin_id, grace_ms=grace_ms,
            )
        except PluginError as exc:
            raise self._plugin_gateway_error(exc) from exc
        return {"plugin": summary.to_dict(), "changed": True}

    async def _plugin_reload(self, command: Command) -> dict:
        plugin_id = str(command.args["id"])
        summaries = await self.host.list_plugins()
        current = next(
            (summary for summary in summaries if summary.id == plugin_id),
            None,
        )
        try:
            if current is not None and current.state == "active":
                await self.host.unload_plugin(plugin_id)
            summary = await self.host.load_plugin(plugin_id)
        except PluginError as exc:
            raise self._plugin_gateway_error(exc) from exc
        return {"plugin": summary.to_dict(), "changed": True}

    async def _session_list(self, command: Command) -> dict:
        limit = int(command.args.get("limit") or 50)
        offset = int(command.args.get("offset") or 0)
        workspace = command.args.get("workspace")
        include_subagents = bool(command.args.get("include_subagents", False))
        infos = await self.host.list_sessions(
            limit=limit, offset=offset, workspace=workspace,
            include_subagents=include_subagents,
        )
        return {
            "sessions": [info.to_dict() for info in infos],
            "limit": limit,
            "offset": offset,
        }

    async def _session_children(self, command: Command) -> dict:
        parent_session_id = command_session_id(command)
        limit = int(command.args.get("limit") or 50)
        offset = int(command.args.get("offset") or 0)
        infos = await self.host.list_children(
            parent_session_id, limit=limit, offset=offset,
        )
        return {
            "parent_session_id": parent_session_id,
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
        children = await self.host.list_children(session_id)
        await self.host.delete_session(session_id)
        # Child gateways are independent subscriptions; reclaim them here so a
        # deleted session cannot keep projecting or broadcasting frames.
        for child in children:
            child_gateway = self._gateways.pop(child.session_id, None)
            if child_gateway is not None:
                child_gateway.broadcast("bye", {"reason": "session deleted"})
                await child_gateway.close()
        logger.info("session %s deleted", session_id)
        return {"deleted": True, "session_id": session_id}

    # -- session gateways ---------------------------------------------------
    async def _gateway_for(self, session_id: str, *,
                           read_only: bool = False,
                           follow: bool = False,
                           allow_approval: bool = False) -> SessionGateway:
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
                follow=follow,
                allow_approval=allow_approval,
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
        """Live gateway when this process owns the session, else read-only.

        A subagent child is never opened as a live session here; it gets a
        read-only, live-following view while the orchestrator owns the reducer
        (design §8.4).
        """
        info = await self.host.get_session_info(session_id)
        if info is not None and info.kind == "subagent":
            return await self._gateway_for(
                session_id, read_only=True, follow=True, allow_approval=True,
            )
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
