"""In-memory event bus: the reference implementation of §14.1 / §15.

``InMemoryEventBus`` wires the pieces together and is the single choke point
through which every event must pass.  The publish pipeline is deliberately
linear and ordered — one function, nine steps — because the correctness of the
whole framework rests on two orderings inside it:

1. **externalise before commit** (P5 / I12) — a crash must never leave an event
   pointing at a blob that does not exist;
2. **WAL before dispatch** (P5) — nothing is delivered that is not durable.

The dispatch loop pulls from the per-origin fair scheduler (I9) and hands each
match to the worker pool; handler failures become dead-letter records rather
than exceptions escaping onto the bus (P4).
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import math
import time
from collections import defaultdict, deque
from dataclasses import replace
from typing import Callable, Iterable, Mapping, Optional

from . import namespace
from .artifact import NullArtifactStore, hydrate_event
from .backpressure import Backpressure
from .coalescer import DeltaCoalescer
from .errors import BusClosed, BusError, PayloadTooLarge
from .event import (
    INLINE_LIMIT,
    MAX_PAYLOAD_BYTES,
    Event,
    dumps,
    now,
)
from .fair_scheduler import OriginFairScheduler
from .metrics import Metrics
from .metrics import metrics as _default_metrics
from .router import Handler, Router, Unsubscribe
from .wal import InMemoryWal
from .error_info import ErrorSource, error_info

DEFAULT_HANDLER_TIMEOUT = 5.0
DEFAULT_DEAD_LETTER_CAP = 1_000

#: Structured-record sink for the observability sidecar (§48).  The bus never
#: depends on any logger; it only fans a plain dict out to registered sinks.
LogSink = Callable[[dict], None]


class DedupSet:
    """Idempotency by ``ev.id`` (§12): the bus is at-least-once, so consumers dedup.

    Default TTL is *None* = session lifetime (dedup lives as long as the bus).
    A bounded ``max_size`` gives a simple LRU-ish eviction for long-lived buses.
    """

    def __init__(
        self,
        ttl: Optional[float] = None,
        max_size: int = 1_000_000,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.ttl = ttl
        self.max_size = max_size
        self._clock = clock
        self._seen: dict[str, float] = {}

    def __contains__(self, event_id: str) -> bool:
        expiry = self._seen.get(event_id)
        if expiry is None:
            return False
        if expiry <= self._clock():
            del self._seen[event_id]
            return False
        return True

    def mark(self, event_id: str) -> None:
        self._seen[event_id] = math.inf if self.ttl is None else self._clock() + self.ttl
        while len(self._seen) > self.max_size:
            self._seen.pop(next(iter(self._seen)))

    def clear(self) -> None:
        self._seen.clear()

    def __len__(self) -> int:
        return len(self._seen)


class InMemoryEventBus:
    """Single-process event bus (§15 reference implementation)."""

    def __init__(
        self,
        wal=None,
        artifacts=None,
        *,
        workers: int = 8,
        max_per_session: int = 4,
        per_origin_cap: int = 2_000,
        handler_timeout: float = DEFAULT_HANDLER_TIMEOUT,
        handler_timeouts: Optional[Mapping[str, Optional[float]]] = None,
        coalesce_window_ms: int = 50,
        coalesce_max_bytes: int = 4096,
        metrics: Optional[Metrics] = None,
        dead_letter_cap: int = DEFAULT_DEAD_LETTER_CAP,
    ) -> None:
        self.wal = wal if wal is not None else InMemoryWal()
        self.artifacts = artifacts if artifacts is not None else NullArtifactStore()
        self.metrics = metrics if metrics is not None else _default_metrics

        self.router = Router()
        self.fair = OriginFairScheduler(per_origin_cap, metrics=self.metrics)
        self.dedup = DedupSet()
        self.coalescer = DeltaCoalescer(coalesce_window_ms, coalesce_max_bytes)
        self.bp = Backpressure()
        self.handler_timeout = handler_timeout
        #: Per-event-type overrides for the generic handler watchdog.  A long-running
        #: subscriber (streaming ``llm.request``, …) declares its own budget so the
        #: generic cap cannot falsely cancel it; see :meth:`set_handler_timeout`.
        self.handler_timeouts: dict[str, Optional[float]] = dict(handler_timeouts or {})

        from .scheduler.pool import WorkerPool

        self.pool = WorkerPool(workers, max_per_session)
        self.seq: dict[str, int] = defaultdict(int)
        self.dead_letters: deque = deque(maxlen=dead_letter_cap)

        #: Optional structured-log sinks (§48).  Empty by default => zero cost;
        #: the observability sidecar registers one via :meth:`add_log_sink`.
        self._log_sinks: list[LogSink] = []

        self._stop = asyncio.Event()
        #: Wakes the idle dispatch loop when work arrives, a delta window
        #: expires, or a stop is requested (§14.1 step ⑨).
        self._wake = asyncio.Event()
        self._running = False
        self._closed = False

    # -- subscription -------------------------------------------------------
    def on(self, pattern: str, handler: Handler, *, origin: str = namespace.CORE) -> Unsubscribe:
        """Subscribe ``handler`` to ``pattern`` (capability-checked, §6 / I8)."""
        namespace.assert_can_subscribe(origin, pattern, metrics=self.metrics)
        unsub = self.router.add(pattern, handler)
        self.metrics.subscription(self.router.gen)
        return unsub

    def on_any(self, handler: Handler, *, origin: str = namespace.CORE) -> Unsubscribe:
        """Observation bypass: receive every event regardless of type (§9)."""
        return self.on("*", handler, origin=origin)

    def set_handler_timeout(self, event_type: str, timeout: Optional[float]) -> None:
        """Give ``event_type`` its own handler budget (``None`` = no cap).

        The generic :attr:`handler_timeout` is a *watchdog* against a stuck handler,
        not a business SLA — so a subscriber that legitimately runs long (the
        streaming ``llm.request`` handler, whose total time is unbounded) declares
        its own budget here instead of raising the global cap for everyone.  The bus
        stays business-agnostic: it never hard-codes any event type.
        """
        if timeout is not None and timeout <= 0:
            raise ValueError("handler timeout must be positive or None")
        self.handler_timeouts[event_type] = timeout

    # -- observability sidecar hooks (§48) ----------------------------------
    def add_log_sink(self, sink: LogSink) -> None:
        """Register a structured-record sink (the run logger attaches here)."""
        if sink not in self._log_sinks:
            self._log_sinks.append(sink)

    def remove_log_sink(self, sink: LogSink) -> None:
        """Detach a previously registered sink (idempotent)."""
        with contextlib.suppress(ValueError):
            self._log_sinks.remove(sink)

    def _emit_log(self, record: dict) -> None:
        """Fan one structured record out to every sink; never let it backfire.

        A logging sink must be unable to break dispatch, so every failure is
        swallowed.  The guard keeps the hot path free when nothing is attached.
        """
        if not self._log_sinks:
            return
        for sink in tuple(self._log_sinks):
            try:
                sink(record)
            except Exception:  # noqa: BLE001 - observability must never break the bus
                pass

    def emit_log(self, record: dict) -> None:
        """Public structured-record hook for host-side layers (e.g. gateway audit).

        Follows the same contract as internal records: a sink may never publish
        or raise into the bus, and with no sink attached this is a cheap no-op.
        """
        self._emit_log(record)

    # -- publish pipeline (§14.1) ------------------------------------------
    async def publish(self, ev: Event) -> Optional[Event]:
        """Commit and enqueue one event (steps ①–⑨ of §14.1).

        Returns the committed event — carrying the host-assigned ``seq`` — or
        ``None`` when the event was deduplicated, shed by backpressure, or
        absorbed into a coalescing window.  Hosts that need a durable receipt
        (the Gateway command result) can therefore confirm *exactly* what was
        written instead of guessing from an id.  Ignoring the return value keeps
        the previous fire-and-forget behaviour.
        """
        await self._ensure_seq(ev.session_id)
        ready = await self._prepare_logged(ev)
        if ready is None:
            return None
        return await self._commit(ready)

    async def publish_many(self, evs: Iterable[Event]) -> list[Event]:
        """Batch publish: one WAL transaction (fewer fsyncs) for the ready set.

        Ordering note: the caller is responsible for passing same-session events
        in ``seq`` order; the batch's internal order is preserved end to end.
        Returns the committed events (deduplicated/shed entries are omitted).
        """
        events = list(evs)
        for session_id in {ev.session_id for ev in events}:
            await self._ensure_seq(session_id)
        ready: list[Event] = []
        for ev in events:
            prepared = await self._prepare_logged(ev)
            if prepared is not None:
                ready.append(prepared)
        if not ready:
            return []
        ready = [self._assign_seq(ev) for ev in ready]
        await self.wal.append_many(ready)  # ⑦ single group commit
        queued = False
        for ev in ready:
            if await self.fair.put(ev):  # ⑨ per-origin enqueue
                self.metrics.published(ev.origin, ev.type)
                queued = True
        if queued:
            self._wake.set()
        return ready

    async def _prepare_logged(self, ev: Event) -> Optional[Event]:
        """``_prepare`` with a publish-rejection record on the way out (§34.1)."""
        try:
            return await self._prepare(ev)
        except BusError as exc:
            self._emit_log(
                {
                    "kind": "publish_error",
                    "level": "warning",
                    "msg": f"publish rejected: {getattr(ev, 'type', '?')} code={exc.code}",
                    "ts": now(),
                    "event_id": getattr(ev, "id", None),
                    "trace_id": getattr(ev, "trace_id", None),
                    "session_id": getattr(ev, "session_id", None),
                    "origin": getattr(ev, "origin", None),
                    "type": getattr(ev, "type", None),
                    "error": str(exc),
                    "code": exc.code,
                    "error_info": exc.error_info().to_dict(),
                }
            )
            raise

    async def _prepare(self, ev: Event) -> Optional[Event]:
        """Steps ①–⑥: validate, namespace, offload, dedup, shed, coalesce.

        Returns the event to commit, or ``None`` when it was absorbed/dropped.
        """
        if self._closed:
            raise BusClosed("publish on a closed bus")

        ev.validate()                                            # type + fields + §8
        namespace.assert_can_publish(ev.origin, ev.type, metrics=self.metrics)  # ①
        ev = await self._maybe_offload(ev)                       # ③ (before WAL!)

        if ev.id in self.dedup:                                  # ④ idempotency
            self.metrics.dup(ev.type)
            return None
        self.dedup.mark(ev.id)

        if self.bp.should_shed(ev, self.fair.pressure()):        # ⑤ backpressure
            self.metrics.shed(ev.origin, ev.type)
            return None

        merged = self.coalescer.maybe_merge(ev)                  # ⑥ delta merge
        if merged is None:
            # deferred into a merge window (counts every coalesced delta)
            self.metrics.coalesced(ev.type)
            self._wake.set()  # the dispatcher must come back at the deadline
            return None
        return merged

    async def _commit(self, ev: Event) -> Event:
        """Steps ⑦–⑨: assign seq, WAL append, per-origin enqueue; return the fact."""
        ev = self._assign_seq(ev)
        started = time.perf_counter()
        await self.wal.append(ev)  # ⑦ 先落盘 (durability before delivery, P5)
        if self._log_sinks:
            self._emit_log(
                {
                    "kind": "wal",
                    "level": "debug",
                    "msg": f"wal append {ev.type} seq={ev.seq}",
                    "ts": now(),
                    "event_id": ev.id,
                    "trace_id": ev.trace_id,
                    "cause_id": ev.cause_id,
                    "session_id": ev.session_id,
                    "seq": ev.seq,
                    "origin": ev.origin,
                    "type": ev.type,
                    "status": "ok",
                    "duration_ms": round((time.perf_counter() - started) * 1000.0, 3),
                }
            )
        if await self.fair.put(ev):  # ⑨ WFQ enqueue (may shed plugin overflow)
            self.metrics.published(ev.origin, ev.type)
            self._wake.set()
        return ev

    async def _ensure_seq(self, session_id: str) -> None:
        """Seed the in-memory counter from the WAL before appending (I10).

        Without this a reopened database would restart at 1 and collide with the
        ``(session_id, seq)`` unique index, silently dropping new events.
        """
        if session_id not in self.seq:
            last_seq = getattr(self.wal, "last_seq", None)
            if last_seq is not None:
                self.seq[session_id] = await last_seq(session_id)
            ids = getattr(self.wal, "event_ids", None)
            if ids is not None:
                for event_id in await ids(session_id):
                    self.dedup.mark(event_id)
            self.seq.setdefault(session_id, 0)

    def _assign_seq(self, ev: Event) -> Event:
        """Assign the session-local monotonic ``seq`` (I10)."""
        n = self.seq[ev.session_id] + 1
        self.seq[ev.session_id] = n
        return replace(ev, seq=n)

    async def _maybe_offload(self, ev: Event) -> Event:
        """Externalise an oversized payload to an artifact before commit (§32.1).

        Ordering is non-negotiable (P5 / I12): the blob is written *first*, then
        the event referencing it.  With no artifact store, payloads up to the
        256 KB hard cap stay inline; above that we fail rather than truncate.
        """
        raw = dumps(ev.payload)
        size = len(raw.encode("utf-8"))
        if size <= INLINE_LIMIT:
            return ev

        if not getattr(self.artifacts, "available", False):
            if size > MAX_PAYLOAD_BYTES:
                raise PayloadTooLarge(
                    f"payload {size}B exceeds {MAX_PAYLOAD_BYTES}B and no artifact store"
                )
            return ev  # inline but within the hard cap

        art = await self.artifacts.put(
            raw.encode("utf-8"), mime="application/json", session_id=ev.session_id
        )
        self.metrics.offloaded(ev.session_id)
        return replace(
            ev,
            payload={
                "artifact": art.ref,
                "size": size,
                "preview": raw[:512],
                "_offloaded": True,
            },
        )

    # -- dispatch loop ------------------------------------------------------
    async def run(self) -> None:
        """Pull events from the fair scheduler and fan them out to handlers.

        Runs until :meth:`close` (or :meth:`stop`) is called; typical hosts run
        it as ``asyncio.create_task(bus.run())``.
        """
        self._running = True
        # A stop may be requested before this task is first scheduled (a host
        # that fails during startup tears down immediately).  Clearing the flag
        # here would lose that request and spin forever.
        try:
            while not self._stop.is_set():
                await self._flush_coalesced()  # deliver merged deltas whose window elapsed
                ev = self.fair.next()
                if ev is None:
                    await self._wait_for_work()
                    continue
                if isinstance(ev.payload, dict) and ev.payload.get("_offloaded"):
                    try:
                        ev = await hydrate_event(ev, self.artifacts)
                    except Exception as exc:  # noqa: BLE001 - durable event, report and continue
                        self.metrics.dead_letter("artifact_hydration")
                        self._dead_letter(
                            ev, f"{type(exc).__name__}: {exc}",
                            "artifact-hydration", exc=exc,
                        )
                        continue
                for handler in self.router.match(ev.type):
                    self.pool.spawn(self._safe(handler, ev), session_id=ev.session_id)
        finally:
            self._running = False

    async def _wait_for_work(self) -> None:
        """Sleep until an event is queued, a delta window expires, or stop.

        The previous ``asyncio.sleep(0)`` spun the loop at 100% CPU whenever
        the scheduler was empty.  Clearing before the re-check is what makes
        the wakeup lossless: a producer either queued before the check (seen
        here) or sets :attr:`_wake` afterwards (wakes the wait).
        """
        self._wake.clear()
        if self._stop.is_set() or self.fair.total() > 0:
            return
        delay = self.coalescer.next_delay()
        if delay is None:
            await self._wake.wait()
        elif delay <= 0:
            return
        else:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._wake.wait(), delay)

    async def _flush_coalesced(self) -> None:
        for ev in self.coalescer.flush_due():
            await self._commit(ev)

    async def _safe(self, handler: Handler, ev: Event) -> None:
        """Run one handler; convert every failure into data (P4), never leak. (I19)"""
        started = time.perf_counter()
        self._emit_dispatch(handler, ev, "begin", None, level="debug")
        try:
            result = handler(ev)
            if inspect.isawaitable(result):
                await self._await_handler(result, ev.type)
            self.metrics.delivered(ev.origin, ev.type)
            self._emit_dispatch(handler, ev, "ok", started)
        except asyncio.CancelledError:
            raise  # I19: a handler must never swallow cancellation
        except (asyncio.TimeoutError, TimeoutError) as exc:
            self.metrics.dead_letter("handler_timeout")
            self._emit_dispatch(handler, ev, "timeout", started)
            self._dead_letter(ev, "handler_timeout", handler, exc=exc)
        except Exception as exc:  # noqa: BLE001 - errors are data, not escapes
            reason = f"{type(exc).__name__}: {exc}"
            self.metrics.dead_letter(reason)
            self._emit_dispatch(handler, ev, "error", started)
            self._dead_letter(ev, reason, handler, exc=exc)

    def _emit_dispatch(self, handler: Handler, ev: Event, status: str,
                       started: Optional[float], *, level: Optional[str] = None) -> None:
        """Structured record for one handler invocation (observability sidecar)."""
        if not self._log_sinks:
            return
        qualname = getattr(handler, "__qualname__", repr(handler))
        duration = None if started is None else round(
            (time.perf_counter() - started) * 1000.0, 3
        )
        self._emit_log(
            {
                "kind": "dispatch",
                "level": level or ("warning" if status in ("error", "timeout") else "info"),
                "msg": f"dispatch {ev.type} -> {qualname} [{status}]",
                "ts": now(),
                "event_id": ev.id,
                "trace_id": ev.trace_id,
                "cause_id": ev.cause_id,
                "session_id": ev.session_id,
                "seq": ev.seq,
                "origin": ev.origin,
                "type": ev.type,
                "handler": qualname,
                "status": status,
                "duration_ms": duration,
            }
        )

    async def _await_handler(self, coro, event_type: str) -> None:
        """Await a handler under its watchdog budget, distinguishing timeout from cancel.

        ``asyncio.wait_for`` cannot tell the handler *why* it was cancelled, so a
        watchdog timeout would surface inside the handler as a bare
        ``CancelledError`` (indistinguishable from a user cancel → mislabeled
        ``E_CANCELLED``).  Here we flag the handler task with ``_handler_timeout``
        before cancelling it, so a subscriber can report ``E_TIMEOUT`` instead.
        """
        timeout = self.handler_timeouts.get(event_type, self.handler_timeout)
        if timeout is None:
            await coro
            return
        task = asyncio.ensure_future(coro)
        try:
            done, _ = await asyncio.wait({task}, timeout=timeout)
        except asyncio.CancelledError:
            task.cancel()                                   # outer cancel: don't orphan the handler
            with contextlib.suppress(BaseException):
                await task
            raise
        if task in done:
            await task                                      # propagate result/exception
            return
        task._handler_timeout = True                        # ★ lets the handler say "timeout", not "cancel"
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        raise asyncio.TimeoutError()

    def _dead_letter(self, ev: Event, reason: str, handler: Handler,
                     exc: BaseException | None = None) -> None:
        """Record a handler failure with structured detail (source=bus dispatch).

        The event already became a fact, so a broken handler cannot publish about
        itself (that would self-flood); it is recorded here instead.  ``error_info``
        says *where* (the handler) and *what* (the original exception).
        """
        qualname = getattr(handler, "__qualname__", repr(handler))
        info = error_info(
            "E_HANDLER_ERROR",
            source=ErrorSource.BUS.value,
            location=qualname,
            message=reason,
            exc=exc,
            enters_history=False,
        )
        self.dead_letters.append(
            {
                "event": ev,
                "reason": reason,
                "handler": qualname,
                "error_info": info.to_dict(),
                "ts": now(),
            }
        )
        if self._log_sinks:
            self._emit_log(
                {
                    "kind": "dead_letter",
                    "level": "error",
                    "msg": f"dead letter: {reason} ({qualname})",
                    "ts": now(),
                    "event_id": ev.id,
                    "trace_id": ev.trace_id,
                    "cause_id": ev.cause_id,
                    "session_id": ev.session_id,
                    "seq": ev.seq,
                    "origin": ev.origin,
                    "type": ev.type,
                    "handler": qualname,
                    "error": reason,
                    "error_info": info.to_dict(),
                }
            )

    # -- lifecycle ----------------------------------------------------------
    def stop(self) -> None:
        """Ask :meth:`run` to return at the next iteration."""
        self._stop.set()
        self._wake.set()  # an idle loop must not sleep through the stop

    async def close(self, drain: bool = True) -> None:
        """Stop dispatch, flush coalesced deltas, optionally drain, close the WAL."""
        if self._closed:
            return
        self._stop.set()
        self._wake.set()
        try:
            for ev in self.coalescer.flush_all():
                await self._commit(ev)
        finally:
            if drain:
                await self.pool.drain()
            await self.wal.close()
            self._emit_log(
                {
                    "kind": "lifecycle",
                    "level": "info",
                    "msg": "bus closed",
                    "ts": now(),
                    "status": "ok",
                }
            )
            self._closed = True

    # -- recovery / introspection ------------------------------------------
    async def replay(self, *, session_id: Optional[str] = None, after_seq: int = 0) -> list[Event]:
        """Read committed events back out of the WAL (§47 recovery primitive)."""
        return await self.wal.replay(session_id=session_id, after_seq=after_seq)

    async def wait_until_idle(self, timeout: float = 5.0, poll: float = 0.001) -> bool:
        """Block until the queues and worker pool are quiescent (test helper)."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            await self._flush_coalesced()
            if self.fair.total() == 0 and self.coalescer.pending == 0 and self.pool.inflight == 0:
                return True
            await asyncio.sleep(poll)
        return False

    @property
    def pending(self) -> int:
        """Events currently queued for dispatch (across all origins)."""
        return self.fair.total()

    @property
    def running(self) -> bool:
        return self._running

    @property
    def closed(self) -> bool:
        return self._closed
