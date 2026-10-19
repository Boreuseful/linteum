"""SessionGateway: one pump + one projector per session (§4.2 / §6).

The gateway never publishes an event on behalf of a client without going
through a whitelisted command, and it never fans out a bus event directly from
the bus handler.  Instead:

``bus.on_any`` (synchronous)
    ``SessionPump.on_event`` only does ``queue.put_nowait`` — the bus handler
    never awaits network I/O (the 5 s watchdog must not misfire, §6.3/§7.7).

``SessionPump`` (one task per session)
    orders events by ``seq``, fills holes from the WAL, projects them and fans
    the resulting frames out to every subscriber.  A gap that the WAL cannot
    fill becomes an explicit ``gap`` frame instead of silent reordering.

Commands are serialised per session (``_command_lock``), user messages are
serialised with the queue release path (``_input_lock``), and repeated
``client_message_id`` values return the original receipt (§6.5).
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional

from ..bus.event import Event
from ..runtime.state import phases
from .errors import (
    E_APPROVAL_GONE,
    E_BAD_FRAME,
    E_BUSY,
    E_INTERNAL,
    E_PROTOCOL,
    E_SESSION_LOCKED,
    E_UNKNOWN_REF,
    GatewayError,
    LOCATION_SESSION,
)
from .log import get_logger
from .projector import ProjectorState, project, snapshot_payload
from .protocol import (
    CONTROL_FRAMES,
    Command,
    Frame,
    command_session_id,
    frame_matches,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..runtime.host import AgentHost
    from ..config.schema import GatewayLimits, HistorySettings
    from .server import ClientConnection

#: Default hole-fill delay before the pump asks the WAL (§6.2).
DEFAULT_GAP_TIMEOUT = 0.020
#: Recent events kept for diagnostics; WAL remains the source of truth.
DEFAULT_RING_SIZE = 2_000
#: Bounded command-idempotency table (§6.5).
DEFAULT_IDEMPOTENCY_SIZE = 1_024
#: One subscription stores at most this many catch-up frames before the replay
#: cutoff is applied; beyond it, the subscriber is told to resubscribe.
CATCHUP_LIMIT = 10_000

logger = get_logger("session")


@dataclass(frozen=True, slots=True)
class QueuedInput:
    """A user message waiting for the session to become idle (§6.6)."""

    text: str
    client_message_id: str
    client_id: Optional[str]
    trace_id: Optional[str]
    metadata: dict = field(default_factory=dict)


@dataclass
class Subscription:
    """One connection's view of one session."""

    key: int
    connection: "ClientConnection"
    mode: str = "view"
    patterns: tuple = ()
    catchup: bool = True
    pending: deque = field(default_factory=deque)
    overflowed: bool = False

    def accepts(self, frame: Frame) -> bool:
        if frame.frame in CONTROL_FRAMES:
            return True
        if self.mode == "events":
            return frame.frame == "event" and frame_matches("event", self.patterns)
        return frame_matches(frame.frame, self.patterns)

    def deliver(self, frame: Frame) -> None:
        if not self.accepts(frame):
            return
        if self.catchup:
            if len(self.pending) < CATCHUP_LIMIT:
                self.pending.append((frame.seq, frame))
            else:
                # Never silently drop durable frames: the subscribe path turns
                # this into an explicit ``gap`` once the replay finishes.
                if not self.overflowed:
                    logger.debug(
                        "connection %d catch-up buffer exceeded %d frames; "
                        "replay will end with a gap",
                        self.connection.id, CATCHUP_LIMIT,
                    )
                self.overflowed = True
            return
        self.connection.send_frame(frame)


class SessionGateway:
    """Ordering, projection, subscriptions and command routing for one session."""

    def __init__(
        self,
        host: "AgentHost",
        session_id: str,
        *,
        limits: Optional["GatewayLimits"] = None,
        history: Optional["HistorySettings"] = None,
        input_policy: str = "queue",
        gap_timeout: float = DEFAULT_GAP_TIMEOUT,
        ring_size: int = DEFAULT_RING_SIZE,
        read_only: bool = False,
    ) -> None:
        if input_policy not in ("queue", "interrupt", "reject"):
            raise ValueError(f"unknown input policy: {input_policy!r}")
        self.host = host
        self.session_id = session_id
        self.bus = host.bus
        self.limits = limits
        self.history_settings = history
        self.input_policy = input_policy
        self.gap_timeout = max(0.0, float(gap_timeout))
        self.ring_size = max(1, int(ring_size))
        #: A read-only gateway replays the WAL but never owns a reducer or a
        #: lease; it serves history/snapshot/artifacts while another process
        #: owns the session (§6.1).
        self.read_only = bool(read_only)
        self._started = False

        self._state = ProjectorState()
        self._queue: asyncio.Queue = asyncio.Queue()
        self._buffer: dict[int, Event] = {}
        self._expected = 1
        #: Serialises the pump task with the WAL hole-filling task.  Both may
        #: observe the same ``expected`` seq; without this lock an event could be
        #: projected twice while the other task sits on an ``await``.
        self._order_lock = asyncio.Lock()
        self._ring: deque = deque(maxlen=self.ring_size)
        self._subscribers: dict[int, Subscription] = {}
        self._unsub = None
        self._pump_task: Optional[asyncio.Task] = None
        self._gap_task: Optional[asyncio.Task] = None
        self._resolving_gap = False
        self._closed = False

        self._command_lock = asyncio.Lock()
        self._input_lock = asyncio.Lock()
        self._input_queue: deque[QueuedInput] = deque()
        #: A user.message was committed but the pump has not projected it yet.
        #: Treat this window as busy so two clients cannot interleave two turns.
        self._published_input_pending = False
        self._pending_input_seq: Optional[int] = None
        self._idempotency: OrderedDict[str, dict] = OrderedDict()
        self._decided: OrderedDict[str, str] = OrderedDict()
        self._artifact_refs: set[str] = set()
        self._dropped_frames = 0

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    async def start(self) -> "SessionGateway":
        """Replay the session, then start the live pump (idempotent).

        The ``on_any`` handler is registered *before* the first ``await`` so no
        event committed during the replay can fall between the two paths.
        """
        if self._started or self._closed:
            return self
        if not self.read_only:
            self._unsub = self.bus.on_any(self._on_event)

        try:
            events = await self.host.replay(self.session_id)
            state = ProjectorState()
            self._ring.clear()
            self._artifact_refs.clear()
            for event in events:
                frames, state = project(event, state)
                self._collect_refs(event)
                self._ring.append((event.seq, tuple(frames)))
                self._remember_committed_message(event)
        except BaseException:
            if self._unsub is not None:
                self._unsub()
                self._unsub = None
            raise

        self._state = state
        self._expected = (events[-1].seq + 1) if events else 1
        self._started = True
        logger.debug(
            "session %s gateway started (read_only=%s, replayed=%d, next_seq=%d)",
            self.session_id, self.read_only, len(events), self._expected,
        )
        if not self.read_only:
            self._pump_task = asyncio.create_task(self._pump())
        return self

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        logger.debug(
            "session %s gateway closing (%d subscriber(s), %d queued input(s))",
            self.session_id, len(self._subscribers), len(self._input_queue),
        )
        if self._unsub is not None:
            self._unsub()
            self._unsub = None
        gap_task, self._gap_task = self._gap_task, None
        if gap_task is not None:
            gap_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await gap_task
        task, self._pump_task = self._pump_task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._subscribers.clear()
        self._input_queue.clear()

    @property
    def state(self) -> ProjectorState:
        return self._state

    def snapshot(self) -> dict:
        return snapshot_payload(self._state)

    def drop_subscriber(self, connection_id: int) -> None:
        """Forget a disconnected client (called by the server on EOF)."""
        self._subscribers.pop(connection_id, None)

    def broadcast(self, frame: str, payload: Optional[dict] = None, *,
                  ephemeral: bool = False, seq: Optional[int] = None) -> None:
        """Send a control frame to every subscriber (e.g. ``bye``)."""
        self._fanout_control(Frame.create(
            frame, payload or {}, session_id=self.session_id,
            ephemeral=ephemeral, seq=seq,
        ))

    # ------------------------------------------------------------------
    # pump
    # ------------------------------------------------------------------
    def _on_event(self, event: Event) -> None:
        """Bus handler: never await, never do I/O (§6.3 / §7.7)."""
        if event.session_id == self.session_id:
            self._queue.put_nowait(event)

    async def _pump(self) -> None:
        while True:
            event = await self._queue.get()
            try:
                await self._accept(event)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - a bad event must not kill the pump
                logger.exception(
                    "session %s failed to project %s seq=%s",
                    self.session_id, getattr(event, "type", None),
                    getattr(event, "seq", None),
                )
                self._log({
                    "kind": "gateway_pump_error",
                    "level": "error",
                    "msg": f"session {self.session_id}: {type(exc).__name__}: {exc}",
                    "ts": time.time(),
                    "session_id": self.session_id,
                    "type": getattr(event, "type", None),
                    "seq": getattr(event, "seq", None),
                })

    async def _accept(self, event: Event) -> None:
        async with self._order_lock:
            await self._accept_locked(event)

    async def _accept_locked(self, event: Event) -> None:
        seq = int(event.seq or 0)
        if seq <= 0 or seq < self._expected:
            return  # duplicate / already projected
        if seq > self._expected:
            self._buffer.setdefault(seq, event)
            self._schedule_gap()
            return
        await self._deliver(event)
        self._expected = seq + 1
        await self._drain_buffer_locked()

    async def _drain_buffer(self) -> None:
        async with self._order_lock:
            await self._drain_buffer_locked()

    async def _drain_buffer_locked(self) -> None:
        while self._expected in self._buffer:
            event = self._buffer.pop(self._expected)
            await self._deliver(event)
            self._expected += 1

    def _schedule_gap(self) -> None:
        if self._closed or self._resolving_gap:
            return
        if self._gap_task is None or self._gap_task.done():
            self._gap_task = asyncio.create_task(self._resolve_gap())

    async def _resolve_gap(self) -> None:
        """Wait briefly, then ask the WAL; still missing → explicit ``gap``."""
        self._resolving_gap = True
        try:
            await asyncio.sleep(self.gap_timeout)
            missing = self._expected
            if missing in self._buffer:
                await self._drain_buffer()
                return
            events = await self.host.replay(self.session_id, after_seq=missing - 1)
            for event in events:
                if self._closed:
                    return
                if event.seq >= missing:
                    await self._accept(event)
            if self._expected != missing:
                return
            last = int(await self.host.last_seq(self.session_id))
            target = min(self._buffer) if self._buffer else max(missing, last + 1)
            self._emit_gap(missing, max(missing, target - 1))
            self._expected = target
            await self._drain_buffer()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - surface as a gap, never hang
            missing = self._expected
            logger.warning(
                "session %s gap resolution failed at seq %d: %s: %s",
                self.session_id, missing, type(exc).__name__, exc,
            )
            self._emit_gap(missing, missing - 1)
            self._log({
                "kind": "gateway_gap_error",
                "level": "error",
                "msg": f"gap resolution failed: {type(exc).__name__}: {exc}",
                "ts": time.time(),
                "session_id": self.session_id,
            })
        finally:
            self._resolving_gap = False
            self._gap_task = None

    def _emit_gap(self, from_seq: int, to_seq: int) -> None:
        if (
            self._pending_input_seq is not None
            and from_seq <= self._pending_input_seq <= max(from_seq, to_seq)
        ):
            # The host input event fell into a loss window; do not keep the
            # session marked busy waiting for a projection that will not come.
            self._published_input_pending = False
            self._pending_input_seq = None
        logger.warning(
            "session %s emitting gap %d..%d (%d event(s) buffered)",
            self.session_id, from_seq, to_seq, len(self._buffer),
        )
        frame = Frame.create(
            "gap",
            {"from_seq": from_seq, "to_seq": to_seq},
            session_id=self.session_id,
            seq=max(from_seq, to_seq),
        )
        self._fanout_control(frame)

    async def _deliver(self, event: Event) -> None:
        frames, state = project(event, self._state)
        self._state = state
        self._collect_refs(event)
        self._ring.append((event.seq, tuple(frames)))
        self._remember_committed_message(event)
        if event.type == "user.message":
            self._published_input_pending = False
            self._pending_input_seq = None
        if event.type == "tool.call.requested":
            call_id = event.payload.get("call_id")
            if call_id:
                self._decided.pop(call_id, None)

        raw = Frame.create(
            "event",
            event.to_dict(),
            session_id=self.session_id,
            seq=event.seq,
            ephemeral=event.type.endswith(".delta"),
        )
        self._fanout(raw, semantic=False)
        for ordinal, projected in enumerate(frames):
            frame = Frame.create(
                projected.name,
                projected.payload,
                session_id=self.session_id,
                seq=event.seq,
                ephemeral=projected.ephemeral,
                ordinal=ordinal,
            )
            self._fanout(frame, semantic=True)

        # Release a queued message only on an event that *definitively* ends the
        # round.  In particular, ``llm.completed`` without tools is not enough:
        # the reducer may still be finishing (and is about to publish
        # ``agent.completed``); publishing the next user message first could
        # make the reducer drop the old final assistant message.
        release = event.type in ("agent.completed", "tool.cancel.requested")
        if event.type == "llm.completed" and event.payload.get("interrupted"):
            release = True
        if release and self._state.phase == phases.IDLE:
            await self._maybe_release_next_input()

    # ------------------------------------------------------------------
    # fan-out
    # ------------------------------------------------------------------
    def _fanout(self, frame: Frame, *, semantic: bool) -> None:
        for subscription in list(self._subscribers.values()):
            if semantic != (subscription.mode == "view"):
                continue
            subscription.deliver(frame)

    def _fanout_control(self, frame: Frame) -> None:
        for subscription in list(self._subscribers.values()):
            subscription.deliver(frame)

    # ------------------------------------------------------------------
    # command dispatch
    # ------------------------------------------------------------------
    async def dispatch(self, cmd: Command, connection: "ClientConnection") -> dict:
        """Route one validated command; returns the ``result`` payload."""
        if self.read_only and cmd.name in (
            "user.message", "user.interrupt", "approval.decide", "agent.resume",
        ):
            raise GatewayError(
                E_SESSION_LOCKED,
                "session is owned by another process; attach to the owner to write",
                location=LOCATION_SESSION,
                retryable=True,
            )
        target = command_session_id(cmd)
        if target is not None and target != self.session_id:
            raise GatewayError(
                E_PROTOCOL,
                "command session_id does not match the addressed session",
                location=LOCATION_SESSION,
            )
        async with self._command_lock:
            if cmd.name == "user.message":
                return await self._user_message(cmd, connection)
            if cmd.name == "user.interrupt":
                self._invalidate_approvals()
                committed = await self._publish(
                    Event.create(
                        "user.interrupt",
                        {"reason": cmd.args.get("reason"), "client_id": connection.id},
                        session_id=self.session_id,
                        trace_id=cmd.trace_id,
                    )
                )
                return _receipt(committed)
            if cmd.name == "approval.decide":
                return await self._approval_decide(cmd, connection)
            if cmd.name == "agent.resume":
                committed = await self._publish(
                    Event.create(
                        "agent.resumed",
                        {"client_id": connection.id},
                        session_id=self.session_id,
                        trace_id=cmd.trace_id,
                    )
                )
                return _receipt(committed)
            if cmd.name == "session.subscribe":
                return await self._subscribe(cmd, connection)
            if cmd.name == "session.unsubscribe":
                removed = self._subscribers.pop(connection.id, None)
                return {"unsubscribed": removed is not None}
            if cmd.name == "history.read":
                return await self._history(cmd, connection)
            if cmd.name == "artifact.read":
                return await self._artifact(cmd, connection)
            if cmd.name == "session.get":
                return {"session_id": self.session_id, "snapshot": self.snapshot()}
        raise GatewayError(
            E_INTERNAL, f"command {cmd.name!r} is not routable to a session",
            location=LOCATION_SESSION,
        )

    # -- user input ------------------------------------------------------
    async def _user_message(self, cmd: Command, connection: "ClientConnection") -> dict:
        args = cmd.args
        text = args["text"]
        client_message_id = args["client_message_id"]

        async with self._input_lock:
            cached = self._idempotency.get(client_message_id)
            if cached is not None:
                self._idempotency.move_to_end(client_message_id)
                logger.debug(
                    "session %s duplicate user.message %s from client=%d",
                    self.session_id, client_message_id, connection.id,
                )
                return {**cached, "duplicate": True}

            if (
                self._state.phase != phases.IDLE
                or self._input_queue
                or self._published_input_pending
            ):
                if self.input_policy == "reject":
                    raise GatewayError(
                        E_BUSY, "session is busy", retryable=True,
                        location=LOCATION_SESSION,
                        context={"phase": self._state.phase},
                    )
                if self.input_policy == "interrupt":
                    self._invalidate_approvals()
                    await self._publish(
                        Event.create(
                            "user.interrupt",
                            {"reason": "user.message", "client_id": connection.id},
                            session_id=self.session_id,
                            trace_id=cmd.trace_id,
                        )
                    )
                self._input_queue.append(QueuedInput(
                    text=text,
                    client_message_id=client_message_id,
                    client_id=connection.id,
                    trace_id=cmd.trace_id,
                    metadata=dict(args.get("metadata") or {}),
                ))
                logger.debug(
                    "session %s queued user.message %s from client=%d "
                    "(phase=%s, pending=%d)",
                    self.session_id, client_message_id, connection.id,
                    self._state.phase, len(self._input_queue),
                )
                record = {
                    "client_message_id": client_message_id,
                    "event_id": None,
                    "seq": None,
                    "queued": True,
                }
                self._remember(client_message_id, record)
                return dict(record)

            self._published_input_pending = True
            try:
                committed = await self._publish_user_message(
                    text=text,
                    client_message_id=client_message_id,
                    client_id=connection.id,
                    trace_id=cmd.trace_id,
                    metadata=dict(args.get("metadata") or {}),
                )
            except BaseException:
                self._published_input_pending = False
                raise
            if committed is None:
                self._published_input_pending = False
            else:
                self._pending_input_seq = int(committed.seq or 0)
            record = {
                "client_message_id": client_message_id,
                **_receipt(committed),
                "queued": False,
            }
            self._remember(client_message_id, record)
            return dict(record)

    async def _publish_user_message(
        self,
        *,
        text: str,
        client_message_id: str,
        client_id: Optional[str],
        trace_id: Optional[str],
        metadata: dict,
    ) -> Optional[Event]:
        payload = {
            "text": text,
            "client_message_id": client_message_id,
            "client_id": client_id,
        }
        if metadata:
            payload["metadata"] = metadata
        return await self._publish(
            Event.create(
                "user.message", payload,
                session_id=self.session_id, trace_id=trace_id,
            )
        )

    async def _maybe_release_next_input(self) -> None:
        """Publish one queued message once the session returns to idle (§6.6)."""
        async with self._input_lock:
            if not self._input_queue or self._state.phase != phases.IDLE:
                return
            item = self._input_queue.popleft()
            self._published_input_pending = True
            try:
                committed = await self._publish_user_message(
                    text=item.text,
                    client_message_id=item.client_message_id,
                    client_id=item.client_id,
                    trace_id=item.trace_id,
                    metadata=item.metadata,
                )
            except GatewayError as exc:
                self._published_input_pending = False
                logger.warning(
                    "session %s could not release queued user.message %s: "
                    "%s: %s",
                    self.session_id, item.client_message_id, exc.code, exc,
                )
                self._remember(item.client_message_id, {
                    "client_message_id": item.client_message_id,
                    "event_id": None,
                    "seq": None,
                    "queued": False,
                    "error": exc.to_dict(),
                })
                self._fanout_control(Frame.create(
                    "notice",
                    {"kind": "input_failed", "error_info": exc.to_dict()},
                    session_id=self.session_id,
                ))
                return
            if committed is None:
                self._published_input_pending = False
            else:
                self._pending_input_seq = int(committed.seq or 0)
            self._remember(item.client_message_id, {
                "client_message_id": item.client_message_id,
                **_receipt(committed),
                "queued": False,
            })

    # -- approval --------------------------------------------------------
    async def _approval_decide(self, cmd: Command, connection: "ClientConnection") -> dict:
        call_id = cmd.args["call_id"]
        raw_decision = cmd.args["decision"]
        decision = "granted" if raw_decision in ("grant", "granted") else "denied"

        if (
            call_id in self._decided
            or call_id not in self._state.pending_approval_ids
            or call_id not in self.host.approvals.pending_ids(self.session_id)
        ):
            raise GatewayError(
                E_APPROVAL_GONE,
                f"approval {call_id!r} is no longer pending",
                location=LOCATION_SESSION,
                context={"call_id": call_id},
            )
        self._decided[call_id] = decision
        while len(self._decided) > 256:
            self._decided.popitem(last=False)
        logger.debug(
            "session %s approval %s %s (client=%d)",
            self.session_id, call_id, decision, connection.id,
        )

        etype = "approval.granted" if decision == "granted" else "approval.denied"
        committed = await self._publish(
            Event.create(
                etype,
                {
                    "call_id": call_id,
                    "reason": cmd.args.get("reason"),
                    "client_id": connection.id,
                },
                session_id=self.session_id,
                trace_id=cmd.trace_id,
            )
        )
        return {
            "call_id": call_id,
            "decision": decision,
            **_receipt(committed),
        }

    # -- subscribe / replay ----------------------------------------------
    async def _subscribe(self, cmd: Command, connection: "ClientConnection") -> dict:
        after_seq = int(cmd.args.get("after_seq") or 0)
        mode = cmd.args.get("mode", "view")
        patterns = tuple(cmd.args.get("frames") or ())
        if len(patterns) > 64 or any(len(p) > 128 for p in patterns):
            raise GatewayError(
                E_BAD_FRAME, "too many or oversized frame filters",
                location=LOCATION_SESSION,
            )
        logger.debug(
            "session %s subscribe client=%d after_seq=%d mode=%s frames=%d",
            self.session_id, connection.id, after_seq, mode, len(patterns),
        )

        subscription = Subscription(
            key=connection.id,
            connection=connection,
            mode=mode,
            patterns=patterns,
            catchup=True,
        )
        self._subscribers[connection.id] = subscription
        connection.resume_session(self.session_id)  # an explicit subscribe lifts a gap pause
        try:
            entries, state = await self._replay_entries(after_seq)
            await connection.send_replay_frame(Frame.create(
                "snapshot",
                snapshot_payload(state),
                session_id=self.session_id,
                seq=state.last_seq,
                replay=True,
            ))
            for event, frames in entries:
                if mode == "events":
                    if event.type.endswith(".delta"):
                        continue  # §6.4: ephemeral deltas are never replayed
                    replay = Frame.create(
                        "event", event.to_dict(), session_id=self.session_id,
                        seq=event.seq, replay=True,
                    )
                    if subscription.accepts(replay):
                        await connection.send_replay_frame(replay)
                    continue
                for ordinal, projected in enumerate(frames):
                    if projected.ephemeral:
                        continue  # §6.4: no delta replay
                    replay = Frame.create(
                        projected.name, projected.payload,
                        session_id=self.session_id, seq=event.seq,
                        ephemeral=False, replay=True, ordinal=ordinal,
                    )
                    if subscription.accepts(replay):
                        await connection.send_replay_frame(replay)

            cutoff = state.last_seq
            delivered_upto = cutoff
            # Keep buffering live frames while the catch-up backlog is flushed:
            # a paced send yields to the pump, and a direct send would let a
            # newer frame overtake the older pending ones.
            while subscription.pending:
                seq, frame = subscription.pending.popleft()
                if seq is None or seq > cutoff:
                    await connection.send_replay_frame(frame)
                    if seq is not None:
                        delivered_upto = max(delivered_upto, seq)
            subscription.catchup = False
            if subscription.overflowed:
                # Live frames arrived faster than the replay could flush; tell
                # the client where to resume instead of silently losing finals.
                to_seq = max(delivered_upto, self._state.last_seq)
                connection.send_frame(Frame.create(
                    "gap",
                    {"from_seq": delivered_upto + 1, "to_seq": to_seq},
                    session_id=self.session_id, seq=to_seq,
                ))
                connection.pause_session(self.session_id)
        except Exception:
            self._subscribers.pop(connection.id, None)
            raise
        return {
            "subscribed": True,
            "mode": mode,
            "after_seq": after_seq,
            "snapshot_seq": state.last_seq,
        }

    async def _replay_entries(self, after_seq: int):
        """Project the WAL from the beginning and return entries after a cursor.

        Replaying the prefix is what makes the snapshot (usage, pending
        approvals, phase) exact; the ring is only a live diagnostic cache.
        """
        state = ProjectorState()
        entries: list = []
        for event in await self.host.replay(self.session_id):
            frames, state = project(event, state)
            if event.seq > after_seq:
                entries.append((event, tuple(frames)))
        return entries, state

    # -- history / artifacts ---------------------------------------------
    async def _history(self, cmd: Command, connection: "ClientConnection") -> dict:
        after_seq = int(cmd.args.get("after_seq") or 0)
        page_size = 100
        max_bytes = 1_048_576
        if self.history_settings is not None:
            page_size = max(1, int(self.history_settings.page_size))
            max_bytes = max(1, int(self.history_settings.max_page_bytes))
        if self.limits is not None:
            # Leave room for the envelope and the embedded snapshot.
            max_bytes = min(
                max_bytes, max(1024, int(self.limits.max_frame_bytes) - 8192)
            )
        limit = min(max(1, int(cmd.args.get("limit") or page_size)), page_size)
        mode = cmd.args.get("mode", "view")

        state = ProjectorState()
        page: list[Frame] = []
        size = 0
        next_seq = after_seq
        has_more = False
        included = 0

        for event in await self.host.replay(self.session_id):
            frames, state = project(event, state)
            if event.seq <= after_seq:
                continue
            if included >= limit:
                has_more = True
                break
            batch: list[Frame] = []
            if mode == "events":
                if event.type.endswith(".delta"):
                    next_seq = event.seq  # never replay ephemeral deltas (§6.4)
                    continue
                batch.append(Frame.create(
                    "event", event.to_dict(), session_id=self.session_id,
                    seq=event.seq, replay=True,
                ))
            else:
                durable = [
                    (ordinal, projected)
                    for ordinal, projected in enumerate(frames)
                    if not projected.ephemeral
                ]
                if not durable:
                    next_seq = event.seq  # no durable view for this event
                    continue
                for ordinal, projected in durable:
                    batch.append(Frame.create(
                        projected.name, projected.payload,
                        session_id=self.session_id, seq=event.seq,
                        ephemeral=False, replay=True, ordinal=ordinal,
                    ))
            batch_size = sum(len(frame.frame) + len(str(frame.payload)) for frame in batch)
            if page and size + batch_size > max_bytes:
                has_more = True
                break
            page.extend(batch)
            size += batch_size
            next_seq = event.seq
            included += 1

        payload = {
            "after_seq": after_seq,
            "next_seq": next_seq,
            "has_more": has_more,
            "mode": mode,
            "snapshot": snapshot_payload(state),
            "frames": [frame.to_dict() for frame in page],
        }
        connection.send_frame(Frame.create(
            "history.page", payload, session_id=self.session_id,
            seq=next_seq if next_seq else None, replay=True,
        ))
        return {
            "after_seq": after_seq,
            "next_seq": next_seq,
            "has_more": has_more,
            "count": included,
            "mode": mode,
        }

    async def _artifact(self, cmd: Command, connection: "ClientConnection") -> dict:
        ref = cmd.args["ref"]
        if ref not in self._artifact_refs:
            raise GatewayError(
                E_UNKNOWN_REF,
                "artifact ref was not produced by this session",
                location=LOCATION_SESSION,
                context={"ref": ref},
            )
        offset = int(cmd.args.get("offset") or 0)
        limit = min(max(1, int(cmd.args.get("limit") or 65_536)), 1_048_576)
        if self.limits is not None:
            # base64 expands by 4/3; keep the resulting frame under the limit.
            limit = min(
                limit,
                max(1024, int(self.limits.max_frame_bytes) * 3 // 4 - 4096),
            )
        artifacts = getattr(self.host.storage, "artifacts", None)
        if artifacts is None:  # pragma: no cover - host always supplies one
            raise GatewayError(E_INTERNAL, "no artifact store configured",
                               location=LOCATION_SESSION)
        data = await artifacts.get(ref[len("art:"):])
        chunk = data[offset: offset + limit]
        eof = offset + len(chunk) >= len(data)
        connection.send_frame(Frame.create(
            "artifact.chunk",
            {
                "ref": ref,
                "offset": offset,
                "limit": limit,
                "size": len(data),
                "encoding": "base64",
                "data": base64.b64encode(chunk).decode("ascii"),
                "eof": eof,
            },
            session_id=self.session_id,
        ))
        return {
            "ref": ref,
            "offset": offset,
            "size": len(data),
            "chunk_bytes": len(chunk),
            "eof": eof,
        }

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def _log(self, record: dict) -> None:
        emit = getattr(self.bus, "emit_log", None)
        if emit is not None:
            emit(record)

    async def _publish(self, event: Event) -> Optional[Event]:
        try:
            return await self.bus.publish(event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - report as a command error
            raise GatewayError(
                E_INTERNAL, f"publish failed: {type(exc).__name__}: {exc}",
                location=LOCATION_SESSION, exc=exc,
            ) from exc

    def _invalidate_approvals(self) -> None:
        """Close the interrupt/decision race before the event is dispatched.

        ``user.interrupt`` is a fact published asynchronously; the reducer's
        ``tool.cancel.requested`` reaches the gate only later.  Marking the
        current approvals as decided synchronously means a ``approval.decide``
        arriving in that window gets ``E_APPROVAL_GONE`` (§A.4), and the gate is
        cleared immediately so a decision can never resume a cancelled call.
        """
        for call_id in self._state.pending_approval_ids:
            self._decided[call_id] = "cancelled"
            while len(self._decided) > 256:
                self._decided.popitem(last=False)
        self.host.approvals.cancel(session_id=self.session_id)

    def _remember(self, client_message_id: str, record: dict) -> None:
        self._idempotency[client_message_id] = dict(record)
        self._idempotency.move_to_end(client_message_id)
        while len(self._idempotency) > DEFAULT_IDEMPOTENCY_SIZE:
            self._idempotency.popitem(last=False)

    def _remember_committed_message(self, event: Event) -> None:
        if event.type != "user.message":
            return
        client_message_id = event.payload.get("client_message_id")
        if not client_message_id:
            return
        existing = self._idempotency.get(client_message_id)
        if existing is not None and existing.get("event_id"):
            return
        self._remember(client_message_id, {
            "client_message_id": client_message_id,
            "event_id": event.id,
            "seq": event.seq,
            "queued": False,
        })

    def _collect_refs(self, event: Event) -> None:
        stack = [event.payload]
        while stack:
            value = stack.pop()
            if isinstance(value, str):
                if value.startswith("art:"):
                    self._artifact_refs.add(value)
            elif isinstance(value, dict):
                stack.extend(value.values())
            elif isinstance(value, (list, tuple)):
                stack.extend(value)


def _receipt(committed: Optional[Event]) -> dict:
    if committed is None:
        return {"event_id": None, "seq": None, "accepted": False}
    return {"event_id": committed.id, "seq": committed.seq, "accepted": True}


__all__ = [
    "CATCHUP_LIMIT",
    "DEFAULT_GAP_TIMEOUT",
    "DEFAULT_IDEMPOTENCY_SIZE",
    "DEFAULT_RING_SIZE",
    "QueuedInput",
    "SessionGateway",
    "Subscription",
]
