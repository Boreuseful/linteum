"""Per-plugin event runtime: subscriptions, publishing, hooks and quotas (§6).

A ``PluginRuntime`` is the only bridge between one committed plugin and the
bus.  It owns the plugin's shared quotas (one token bucket, one concurrency
semaphore, one consecutive-failure counter, one namespace-violation counter),
stamps every published event with ``origin="plugin:<id>"`` / ``priority=7``
and the current handler's session/cause context, and converts bus failures
into data: nothing it raises can escape into the bus dispatcher except
``asyncio.CancelledError`` (§6.6).

The host (:class:`~linteum_agent.runtime.plugins.host.PluginHost`) constructs
the runtime before the ``PluginContext``, calls :meth:`install` at commit, and
drives hooks globally through :meth:`invoke_hook_handler` (§6.5).
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import inspect
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional

from ...bus import namespace
from ...bus.errors import CapabilityDenied, NamespaceViolation
from ...bus.event import Event, dumps
from .context import matches_publish_patterns
from .errors import (
    PluginError,
    PluginLoadError,
    PluginNamespaceError,
    PluginTimeout,
    as_plugin_error,
)
from .model import PluginEvent, PluginOrdering, PluginRuntimeLimits

#: Fixed namespace-violation quarantine threshold (§6.7); consecutive-failure
#: quarantine uses ``limits.max_consecutive_failures``.
NAMESPACE_VIOLATION_THRESHOLD = 3

#: Abandoned synchronous handler threads (a ``to_thread`` call whose watchdog
#: fired) before the plugin is quarantined — mirrors the loader's
#: ``MAX_ABANDONED_LOADERS`` (§6.6).  The counter is monotonic: a success
#: never resets it.
MAX_ABANDONED_HANDLER_THREADS = 8

#: Stable ``location`` embedded in hook/subscription failure ``error_info``.
LOCATION_RUNTIME = "linteum_agent.runtime.plugins.events.PluginRuntime"

#: Context variables carrying the triggering event's session/cause into a
#: handler, so ``ctx.publish`` can stamp the causal chain without bus access.
_current_session: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "plugin_current_session", default=None,
)
_current_cause: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "plugin_current_cause", default=None,
)


class TokenBucket:
    """Thread-safe, non-blocking rate limiter (§6.7).

    Tokens refill continuously at ``rate`` per second up to ``burst`` (the
    default is one second worth of rate); :meth:`take` never blocks and never
    raises — callers shed the work when it returns ``False``.
    """

    __slots__ = ("_rate", "_burst", "_tokens", "_updated", "_clock", "_lock")

    def __init__(
        self,
        rate: int,
        *,
        burst: Optional[int] = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._rate = float(rate)
        self._burst = float(rate if burst is None else burst)
        self._tokens = self._burst
        self._updated = clock()
        self._clock = clock
        self._lock = threading.Lock()

    def take(self, cost: int = 1) -> bool:
        """Consume ``cost`` tokens if available; never blocks or raises."""
        if cost < 1:
            raise ValueError(f"token cost must be >= 1, got {cost}")
        with self._lock:
            now = self._clock()
            self._tokens = min(
                self._burst,
                self._tokens + (now - self._updated) * self._rate,
            )
            self._updated = now
            if self._tokens >= cost:
                self._tokens -= cost
                return True
            return False


@dataclass(frozen=True, slots=True)
class HookInvocation:
    """Outcome of one guarded hook-handler invocation (§6.5)."""

    status: str  #: "ok" | "shed" | "failed" | "skipped"
    reason: str = ""
    output: Any = None
    error: Optional[BaseException] = None
    error_info: Optional[dict] = None


class PluginRuntime:
    """Quota-enforcing bridge between one plugin and the bus (§6.3–§6.7).

    One instance per loaded plugin; the shared bucket, semaphore and counters
    cover publishing, subscriptions and hooks alike (§6.7).  The host stamps
    every event (``origin``/``seq``/``session_id``) — the plugin never does.
    """

    def __init__(
        self,
        plugin_id: str,
        bus,
        *,
        host_partition: str,
        limits: PluginRuntimeLimits,
        ordering: PluginOrdering,
        publish_patterns: tuple[str, ...] = (),
        subscribe_patterns: tuple[str, ...] = (),
        is_active: Callable[[], bool],
        on_failure: Optional[Callable[[str, BaseException], Any]] = None,
        on_namespace_violation: Optional[Callable[[int], Any]] = None,
        log: Optional[Callable[[dict], None]] = None,
    ) -> None:
        self.plugin_id = plugin_id
        self.origin = f"plugin:{plugin_id}"
        self._bus = bus
        self.host_partition = host_partition
        self.limits = limits
        self.ordering = ordering
        self._publish_patterns = tuple(publish_patterns)
        self._subscribe_patterns = tuple(subscribe_patterns)
        self._is_active = is_active
        self._on_failure = on_failure
        self._on_namespace_violation = on_namespace_violation
        self._log_sink = log

        # Shared quotas: one bucket, one semaphore, two counters (§6.7).
        self.bucket = TokenBucket(limits.max_events_per_sec)
        self.semaphore = asyncio.Semaphore(limits.max_concurrency)
        self._failure_count = 0
        self._violation_count = 0
        # Monotonic abandoned-thread count (never reset on success, §6.6).
        self._abandoned_threads = 0

        self._hook_handlers: dict[str, tuple[Callable[..., Any], ...]] = {}
        self._unsubscribers: list[Callable[[], None]] = []
        self._closed = False

    # -- install / teardown ------------------------------------------------
    def install(
        self,
        subscriptions: tuple[tuple[str, Callable[..., Any]], ...],
        hooks: dict[str, tuple[Callable[..., Any], ...]],
    ) -> tuple[Callable[[], None], ...]:
        """Register bus subscriptions and adopt hook handlers at commit (§9.1).

        ``subscriptions`` is ``Staging.subscriptions()`` (ordered ``(pattern,
        handler)`` pairs); ``hooks`` is ``Staging.hooks()``.  Every bus
        subscription runs through a quota-enforcing wrapper registered under
        the plugin's own origin.  Returns the stored unsubscribers.
        """
        self._hook_handlers = {
            name: tuple(handlers) for name, handlers in hooks.items()
        }
        unsubscribers: list[Callable[[], None]] = []
        if self._bus is None:
            return tuple(unsubscribers)
        for pattern, handler in subscriptions:
            if pattern not in self._subscribe_patterns:
                # Defense in depth: the manifest and ctx.on already validate,
                # but a mismatched runtime must never register undeclared
                # subscriptions (§6.3).
                raise PluginNamespaceError(
                    f"subscription {pattern!r} is not declared in subscribes",
                    key="subscribes",
                    hint="declare it under subscribes in plugin.yaml",
                    context={"plugin_id": self.plugin_id, "type": pattern},
                )
            wrapper = self._subscription_wrapper(pattern, handler)
            unsubscriber = self._bus.on(pattern, wrapper, origin=self.origin)
            # Stored immediately so a later registration failure cannot leak
            # the earlier ones (§9.1).
            self._unsubscribers.append(unsubscriber)
            unsubscribers.append(unsubscriber)
        return tuple(unsubscribers)

    def hook_handlers(self, name: str) -> tuple[Callable[..., Any], ...]:
        """Handlers registered for one hook, in registration order (§6.5)."""
        return self._hook_handlers.get(name, ())

    async def close(self) -> None:
        """Idempotent teardown: revoke every bus subscription (§9.2)."""
        if self._closed:
            return
        self._closed = True
        for unsubscribe in self._unsubscribers:
            with contextlib.suppress(Exception):
                unsubscribe()
        self._unsubscribers.clear()
        self._hook_handlers = {}

    # -- logging / quota helpers --------------------------------------------
    def _log(self, record: dict) -> None:
        if self._log_sink is None:
            return
        with contextlib.suppress(Exception):
            self._log_sink(record)

    def _log_shed(self, etype: str, reason: str) -> None:
        self._log({
            "kind": "plugin", "level": "warning",
            "msg": f"plugin.shed: {self.plugin_id} {etype} ({reason})",
            "ts": time.time(),
        })

    async def _emit(self, etype: str, payload: dict) -> None:
        """Publish a core-origin observation on the host partition (§6.8)."""
        if self._bus is None:
            return
        try:
            await self._bus.publish(Event.create(
                etype, payload, session_id=self.host_partition,
            ))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - observation, not state
            self._log({
                "kind": "plugin", "level": "warning",
                "msg": f"{etype} publish failed: {type(exc).__name__}: {exc}",
                "ts": time.time(),
            })

    def _effective_timeout_ms(self, event_type: str) -> float:
        """Subscription watchdog budget: the bus's per-type/default cap
        (seconds, ``None`` = uncapped) capped by the plugin's own limit (§6.6).

        The plugin runtime never writes bus budgets; it only reads them.
        """
        timeouts = getattr(self._bus, "handler_timeouts", {}) or {}
        default = getattr(self._bus, "handler_timeout", None)
        budget = timeouts.get(event_type, default)
        if budget is None:
            return float(self.limits.max_handler_ms)
        return min(self.limits.max_handler_ms, budget * 1000.0)

    # -- failure / violation accounting --------------------------------------
    async def record_failure(self, reason: str, exc: BaseException) -> None:
        """Count one consecutive failure; quarantine at the limit (§6.7).

        Failures are shared across hooks, subscriptions and publishing.  The
        host callback runs only when the threshold is crossed, and only while
        the plugin is still active (idempotence of the teardown path).  The
        descriptive ``reason`` rides on the error's context; the callback
        receives the stable reason ``"consecutive_failures"``.
        """
        if self._is_active is not None and not self._is_active():
            return
        self._failure_count += 1
        if self._failure_count < self.limits.max_consecutive_failures:
            return
        if isinstance(exc, PluginError) and not exc.context.get("failure_reason"):
            exc.context["failure_reason"] = reason
        await self._call_host(self._on_failure, "consecutive_failures", exc)

    async def _count_abandoned_thread(self, what: str) -> None:
        """Account a ``to_thread`` handler whose watchdog fired (§6.6).

        The thread keeps running (Python cannot kill it); the count is
        monotonic — success never resets it — and reaching
        :data:`MAX_ABANDONED_HANDLER_THREADS` quarantines the plugin.
        """
        self._abandoned_threads += 1
        self._log({
            "kind": "plugin", "level": "warning",
            "msg": f"plugin.handler_thread_abandoned: {self.plugin_id}",
            "ts": time.time(),
            "handler": what,
            "count": self._abandoned_threads,
        })
        if self._abandoned_threads >= MAX_ABANDONED_HANDLER_THREADS:
            error = PluginLoadError(
                f"{self._abandoned_threads} handler threads were abandoned",
                key="max_handler_ms",
                hint="the plugin blocks worker threads beyond its budget",
            )
            await self._call_host(self._on_failure, "abandoned_threads", error)

    async def _count_violation(self, etype: str) -> None:
        """Count one namespace violation and emit the observation (§6.3)."""
        self._violation_count += 1
        count = self._violation_count
        await self._emit("system.plugin.namespace_violation", {
            "id": self.plugin_id,
            "type": etype,
            "count": count,
        })
        await self._call_host(self._on_namespace_violation, count)

    async def _call_host(
        self, callback: Optional[Callable[..., Any]], *args: Any,
    ) -> None:
        """Await a host callback; a callback failure must never reach the bus."""
        if callback is None:
            return
        try:
            result = callback(*args)
            if inspect.isawaitable(result):
                await result
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - quarantine must not kill dispatch
            self._log({
                "kind": "plugin", "level": "warning",
                "msg": (
                    f"plugin.runtime_callback_failed: {self.plugin_id}: "
                    f"{type(exc).__name__}: {exc}"
                ),
                "ts": time.time(),
            })

    # -- publishing ----------------------------------------------------------
    async def publish(self, etype: str, payload: Mapping[str, Any]) -> bool:
        """Publish one plugin event, host-stamped and quota-checked (§6.3).

        Returns ``False`` when shed by the rate limit, oversize, deduplicated
        by the bus or refused by the namespace (counted and reported as
        ``system.plugin.namespace_violation``).  A type outside the manifest's
        ``publishes`` raises :class:`PluginNamespaceError` (defense in depth —
        ``ctx.publish`` already checked), and a payload that is not pure JSON
        raises :class:`PluginLoadError`.  The subscription wrapper counts both
        escapes, so nothing reaches the bus dispatcher except
        ``CancelledError``.
        """
        if self._closed or (self._is_active is not None and not self._is_active()):
            return False
        if not matches_publish_patterns(etype, self._publish_patterns):
            raise PluginNamespaceError(
                f"event type {etype!r} is not declared in publishes",
                key="publishes",
                hint="declare it under publishes in plugin.yaml",
                context={"plugin_id": self.plugin_id, "type": etype},
            )
        try:
            namespace.assert_can_publish(self.origin, etype)
        except (NamespaceViolation, CapabilityDenied):
            await self._count_violation(etype)
            return False
        try:
            data = dict(payload)
            raw = dumps(data)
        except (TypeError, ValueError) as exc:
            raise PluginLoadError(
                f"publish payload is not pure JSON: {type(exc).__name__}",
                key="publish", detail=str(exc), exc_type=type(exc).__name__,
            ) from exc
        if len(raw.encode("utf-8")) > self.limits.max_output_bytes:
            self._log({
                "kind": "plugin", "level": "warning",
                "msg": (
                    f"plugin.publish_oversize: {self.plugin_id} {etype} "
                    f"exceeds max_output_bytes"
                ),
                "ts": time.time(),
            })
            return False
        if not self.bucket.take():
            self._log_shed(etype, "rate_limit")
            return False
        session_id = _current_session.get() or self.host_partition
        try:
            committed = await self._bus.publish(Event.create(
                etype, data,
                session_id=session_id,
                origin=self.origin,
                cause_id=_current_cause.get(),
                priority=7,
            ))
        except asyncio.CancelledError:
            raise
        except (NamespaceViolation, CapabilityDenied):
            # Defense in depth: the local check above should already have
            # caught this, but a bus-side refusal counts the same way (§6.3).
            await self._count_violation(etype)
            return False
        except Exception as exc:  # noqa: BLE001 - bus trouble is not a plugin failure
            self._log({
                "kind": "plugin", "level": "warning",
                "msg": (
                    f"plugin.publish_failed: {self.plugin_id} {etype}: "
                    f"{type(exc).__name__}: {exc}"
                ),
                "ts": time.time(),
            })
            return False
        return committed is not None

    # -- guarded handler invocation -------------------------------------------
    def _subscription_wrapper(
        self, pattern: str, handler: Callable[..., Any],
    ) -> Callable[..., Any]:
        """Bus handler: convert ``Event`` → ``PluginEvent``, then guard (§6.6)."""

        async def wrapper(event: Any) -> None:
            if self._closed or (self._is_active is not None
                                and not self._is_active()):
                return
            plugin_event = PluginEvent(
                type=event.type,
                session_id=event.session_id,
                payload=event.payload,
                seq=event.seq,
                origin=event.origin,
                ts=event.ts,
                cause_id=event.cause_id,
                trace_id=event.trace_id,
            )
            # The handled event is the cause of whatever the plugin publishes
            # in reaction (§6.3).
            await self._invoke_guarded(
                handler, plugin_event,
                timeout_ms=self._effective_timeout_ms(event.type),
                what=f"subscription {pattern}",
                cause=event.id,
            )

        return wrapper

    async def invoke_hook_handler(
        self,
        name: str,
        handler: Callable[..., Any],
        payload: Mapping[str, Any],
        *,
        session_id: str,
        cause_id: Optional[str] = None,
        trace_id: Optional[str] = None,
    ) -> HookInvocation:
        """Run one hook handler under this plugin's quotas (§6.5).

        The handler receives ``type="hook.<name>"`` and the input payload;
        ``cause_id`` points at the triggering event.  Non-``None`` output is
        validated as pure JSON within ``max_output_bytes`` — a violation is a
        counted failure.  The host publishes the ``hook.<name>.applied`` /
        ``hook.<name>.failed`` facts from the returned outcome.
        """
        plugin_event = PluginEvent(
            type=f"hook.{name}",
            session_id=session_id,
            payload=payload,
            origin="core",
            cause_id=cause_id,
            trace_id=trace_id,
        )
        outcome = await self._invoke_guarded(
            handler, plugin_event,
            timeout_ms=self.limits.max_handler_ms,
            what=f"hook.{name}",
        )
        if outcome.status != "ok" or outcome.output is None:
            return outcome
        try:
            raw = dumps(outcome.output)
        except (TypeError, ValueError) as exc:
            error = PluginLoadError(
                f"hook output is not pure JSON: {type(exc).__name__}",
                key="max_output_bytes", detail=str(exc),
                exc_type=type(exc).__name__,
            )
            await self.record_failure(f"hook.{name} output is not JSON", error)
            return HookInvocation(
                "failed", reason="non_json", error=error,
                error_info=error.to_error_info(location=LOCATION_RUNTIME),
            )
        if len(raw.encode("utf-8")) > self.limits.max_output_bytes:
            error = PluginLoadError(
                f"hook output exceeds max_output_bytes "
                f"({self.limits.max_output_bytes})",
                key="max_output_bytes",
            )
            await self.record_failure(f"hook.{name} output oversize", error)
            return HookInvocation(
                "failed", reason="oversize", error=error,
                error_info=error.to_error_info(location=LOCATION_RUNTIME),
            )
        return outcome

    async def run_hook(
        self,
        name: str,
        payload: Mapping[str, Any],
        *,
        session_id: str,
        cause_id: Optional[str] = None,
        trace_id: Optional[str] = None,
    ) -> None:
        """Invoke this plugin's own handlers for ``name`` in order (§6.5).

        The host drives hooks globally across plugins through
        :meth:`invoke_hook_handler`; this loop is the single-plugin
        convenience (no events are published here).
        """
        for handler in self.hook_handlers(name):
            await self.invoke_hook_handler(
                name, handler, payload,
                session_id=session_id, cause_id=cause_id, trace_id=trace_id,
            )

    async def _invoke_guarded(
        self,
        handler: Callable[..., Any],
        plugin_event: PluginEvent,
        *,
        timeout_ms: float,
        what: str,
        cause: Optional[str] = None,
    ) -> HookInvocation:
        """Quota-check, context-stamp and watchdog one handler call (§6.6).

        Shedding never counts as a failure; timeout, namespace escape and
        plain exceptions do.  Only ``CancelledError`` may escape.
        """
        if self._closed or (self._is_active is not None and not self._is_active()):
            return HookInvocation("skipped", reason="inactive")
        if not self.bucket.take():
            self._log_shed(plugin_event.type, "rate_limit")
            return HookInvocation("shed", reason="rate_limit")
        # Single-loop safety: no await between locked() and acquire(), so the
        # check-then-acquire cannot race.  If handlers ever run on several
        # loops, this needs a timeout-guarded acquire instead.
        if self.semaphore.locked():
            self._log_shed(plugin_event.type, "concurrency")
            return HookInvocation("shed", reason="concurrency")

        session_token = _current_session.set(plugin_event.session_id)
        cause_token = _current_cause.set(
            plugin_event.cause_id if cause is None else cause
        )
        try:
            await self.semaphore.acquire()
            try:
                return await self._call_handler(
                    handler, plugin_event,
                    timeout_ms=timeout_ms, what=what,
                )
            finally:
                self.semaphore.release()
        finally:
            _current_session.reset(session_token)
            _current_cause.reset(cause_token)

    async def _call_handler(
        self,
        handler: Callable[..., Any],
        plugin_event: PluginEvent,
        *,
        timeout_ms: float,
        what: str,
    ) -> HookInvocation:
        timeout = max(0.001, timeout_ms / 1000.0)
        sync = not inspect.iscoroutinefunction(handler)
        try:
            if not sync:
                output = await asyncio.wait_for(handler(plugin_event), timeout)
            else:
                # Synchronous handlers run in a worker thread and may only
                # return output — ``ctx.publish`` is async (§6.3).
                output = await asyncio.wait_for(
                    asyncio.to_thread(handler, plugin_event), timeout,
                )
                if inspect.isawaitable(output):
                    # A sync handler attempted an async call; the coroutine can
                    # never run, so close it to avoid an un-awaited warning.
                    with contextlib.suppress(Exception):
                        output.close()
                    self._log({
                        "kind": "plugin", "level": "warning",
                        "msg": (
                            f"plugin.sync_handler_async_call: {self.plugin_id} "
                            f"{what} returned a coroutine"
                        ),
                        "ts": time.time(),
                    })
                    output = None
        except asyncio.CancelledError:
            raise  # never swallow cancellation (I19)
        except TimeoutError as exc:
            if sync:
                # The thread keeps running — Python cannot kill it; count it
                # so the host can quarantine a blocking plugin (§6.6).
                await self._count_abandoned_thread(what)
            error = PluginTimeout(
                f"{what} timed out after {int(timeout_ms)} ms",
                key="max_handler_ms",
                detail=str(exc), exc_type=type(exc).__name__,
            )
            await self.record_failure(f"{what} timed out", error)
            return HookInvocation(
                "failed", reason="timeout", error=error,
                error_info=error.to_error_info(location=LOCATION_RUNTIME),
            )
        except PluginNamespaceError as exc:
            # ctx.publish validates the manifest patterns locally and raises
            # before the runtime; route the escape to the violation counter so
            # the planned event is emitted (§6.3), not a generic failure.
            await self._count_violation(
                exc.context.get("type") or plugin_event.type
            )
            return HookInvocation(
                "failed", reason="namespace_violation", error=exc,
                error_info=exc.to_error_info(location=LOCATION_RUNTIME),
            )
        except Exception as exc:  # noqa: BLE001 - failures are data (§6.6)
            wrapped = as_plugin_error(exc)
            reason = f"{what} failed: {type(exc).__name__}: {exc}"
            await self.record_failure(reason, wrapped)
            return HookInvocation(
                "failed", reason="exception", error=wrapped,
                error_info=wrapped.to_error_info(location=LOCATION_RUNTIME),
            )
        # A completed handler resets the consecutive-failure counter (§6.7).
        self._failure_count = 0
        return HookInvocation("ok", output=output)


__all__ = [
    "HookInvocation",
    "LOCATION_RUNTIME",
    "MAX_ABANDONED_HANDLER_THREADS",
    "NAMESPACE_VIOLATION_THRESHOLD",
    "PluginRuntime",
    "TokenBucket",
]
