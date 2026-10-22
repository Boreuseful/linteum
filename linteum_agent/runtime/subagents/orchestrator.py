"""SubagentOrchestrator: the sole ``subagent.spawn.requested`` subscriber.

The orchestrator owns the *lifecycle* of delegated work, never the parent's
reducer state:

* validate the host boundary (features, depth, tools, model, budget, queue);
* open an isolated child session through
  :meth:`~linteum_agent.runtime.host.AgentHost.open_subagent_session`;
* publish ``subagent.started`` / ``progress`` metadata on the **parent**
  session, then ``subagent.completed`` / ``subagent.failed`` exactly once;
* cancel, time out and clean up without ever calling a provider after the
  terminal is decided.

Bus handlers are short.  ``on_spawn`` validates and schedules; the actual wait
happens in ``_run_task`` so the 5 s handler watchdog cannot kill a long child.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

from ...bus.error_info import ErrorSource, error_info
from ...bus.event import Event
from ...config.schema import SubagentSettings
from ..state import phases
from .catalog import SubagentCatalog, normalise_title
from .errors import (
    E_SUBAGENT_BUDGET,
    E_SUBAGENT_BUSY,
    E_SUBAGENT_CANCELLED,
    E_SUBAGENT_DEPTH,
    E_SUBAGENT_DISABLED,
    E_SUBAGENT_FAILED,
    E_SUBAGENT_INVALID,
    E_SUBAGENT_SPAWN,
    E_SUBAGENT_TIMEOUT,
    SubagentError,
)
from .model import SubagentPolicy, SubagentTask
from .summary import EMPTY_SUMMARY, build_summary, extract_summary, truncate_utf8

LOCATION_ORCHESTRATOR = (
    "linteum_agent.runtime.subagents.orchestrator.SubagentOrchestrator"
)


@dataclass
class _ActiveChild:
    """One running child, owned by the orchestrator's ``_run_task``."""

    call_id: str
    parent_session_id: str
    child_session_id: str
    spawn_event_id: str
    trace_id: str
    task: SubagentTask
    policy: SubagentPolicy
    started_at: float
    done: asyncio.Event = field(default_factory=asyncio.Event)
    terminal: Optional[Event] = None
    cancel_requested: bool = False
    self_interrupt: bool = False
    terminal_sent: bool = False
    status: str = "running"
    last_progress: float = 0.0
    last_tool: Optional[str] = None
    tokens_input: int = 0
    tokens_output: int = 0


class SubagentOrchestrator:
    """Host-assembled effect layer for delegated child sessions (§7)."""

    def __init__(
        self,
        bus,
        host,
        settings: SubagentSettings,
        *,
        artifacts=None,
        clock=time.time,
    ) -> None:
        self.bus = bus
        self.host = host
        self.settings = settings
        self.artifacts = (
            artifacts if artifacts is not None
            else getattr(bus, "artifacts", None)
        )
        self.clock = clock
        self.catalog = SubagentCatalog(settings)

        self._active: dict[str, _ActiveChild] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._task_parent: dict[str, str] = {}
        self._semaphores: dict[str, asyncio.Semaphore] = {}
        self._waiting: dict[str, int] = {}
        #: Tasks holding a concurrency slot, per parent (includes the short
        #: window between ``acquire`` and ``_active`` registration).
        self._running: dict[str, int] = {}
        self._cancel_requested: set[str] = set()
        self._closed = False
        self._unsubs = [
            bus.on("subagent.spawn.requested", self.on_spawn),
            bus.on("subagent.cancel.requested", self.on_cancel),
            bus.on("agent.completed", self.on_child_completed),
            bus.on("system.budget.exceeded", self.on_child_budget),
            bus.on("user.interrupt", self.on_child_interrupt),
            bus.on("llm.started", self.on_child_event),
            bus.on("tool.call.started", self.on_child_event),
            bus.on("tool.call.completed", self.on_child_event),
            bus.on("tool.call.failed", self.on_child_event),
            bus.on("llm.completed", self.on_child_usage),
        ]

    # -- subscription handlers (§7.1) --------------------------------------
    async def on_spawn(self, ev: Event) -> None:
        """Validate one spawn request and schedule its supervisor task."""
        payload = ev.payload or {}
        call_id = str(payload.get("call_id") or "")
        title = normalise_title(payload.get("title"), payload.get("task") or "")
        context = {
            "parent_session_id": ev.session_id,
            "call_id": call_id,
        }
        try:
            if not self.settings.enabled:
                raise SubagentError(
                    E_SUBAGENT_DISABLED, "subagents are disabled by the host",
                    context=context,
                )
            if self._closed:
                raise SubagentError(
                    E_SUBAGENT_CANCELLED, "orchestrator is shutting down",
                    context=context,
                )
            parent = self.host.session(ev.session_id)
            if parent is None:
                raise SubagentError(
                    E_SUBAGENT_INVALID, "parent session is not open",
                    context=context,
                )
            if parent.kind != "root":
                raise SubagentError(
                    E_SUBAGENT_DEPTH, "subagents cannot spawn subagents",
                    context=context,
                )
            if self.settings.max_depth < 1:
                raise SubagentError(
                    E_SUBAGENT_DEPTH,
                    f"max_depth={self.settings.max_depth} forbids spawning",
                    context=context,
                )
            if not call_id:
                raise SubagentError(
                    E_SUBAGENT_INVALID, "spawn request is missing call_id",
                    context=context,
                )

            effects = self.host.subagent_tool_specs(ev.session_id)
            profiles = self.host.current_config().agent.profile_names()
            policy = self.catalog.resolve(
                call_id, payload,
                parent_tools=tuple(effects),
                tool_side_effects=effects,
                profiles=profiles,
            )
            queued = self._waiting.get(ev.session_id, 0)
            running = self._running.get(ev.session_id, 0)
            would_queue = (
                running >= self.settings.max_concurrent_per_session or queued > 0
            )
            if would_queue and queued >= self.settings.max_queued_per_session:
                raise SubagentError(
                    E_SUBAGENT_BUSY,
                    "subagent queue is full for this parent session",
                    context={**context,
                             "queued": queued,
                             "running": running,
                             "max_concurrent_per_session":
                                 self.settings.max_concurrent_per_session,
                             "max_queued_per_session":
                                 self.settings.max_queued_per_session},
                )

            task = SubagentTask(
                call_id=call_id,
                parent_session_id=ev.session_id,
                spawn_event_id=ev.id,
                trace_id=ev.trace_id,
                title=title,
                task=str(payload.get("task", "")),
                instructions=str(payload.get("instructions", "")),
                context=str(payload.get("context") or ""),
                environment=str(payload.get("environment") or ""),
                expected_output=str(payload.get("expected_output") or ""),
                policy=policy,
            )
            self._waiting[ev.session_id] = self._waiting.get(ev.session_id, 0) + 1
            self._task_parent[call_id] = ev.session_id
            supervisor = asyncio.create_task(self._run_task(task, ev))
            self._tasks[call_id] = supervisor
            supervisor.add_done_callback(
                lambda _t, cid=call_id: self._tasks.pop(cid, None)
            )
        except SubagentError as exc:
            await self._fail_spawn(call_id, context, exc, title=title)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - errors are data (P4)
            await self._fail_spawn(
                call_id, context,
                SubagentError(E_SUBAGENT_FAILED,
                              f"{type(exc).__name__}: {exc}", context=context),
                title=title,
            )

    def on_cancel(self, ev: Event) -> None:
        """Fast cancellation marker: never waits for a child (§7.7)."""
        call_ids = list((ev.payload or {}).get("call_ids") or ())
        if call_ids:
            for call_id in call_ids:
                active = self._by_call(call_id)
                if active is not None:
                    self._signal_cancel(active)
                else:
                    self._cancel_requested.add(call_id)
            return
        for active in list(self._active.values()):
            if active.parent_session_id == ev.session_id:
                self._signal_cancel(active)
        for call_id, parent_id in list(self._task_parent.items()):
            if parent_id == ev.session_id:
                self._cancel_requested.add(call_id)

    def on_child_completed(self, ev: Event) -> None:
        active = self._active.get(ev.session_id)
        if active is None:
            return
        active.terminal = ev
        active.done.set()

    def on_child_budget(self, ev: Event) -> None:
        active = self._active.get(ev.session_id)
        if active is None:
            return
        active.terminal = ev
        active.done.set()

    def on_child_interrupt(self, ev: Event) -> None:
        active = self._active.get(ev.session_id)
        if active is None or active.self_interrupt:
            return
        # Somebody interrupted the child directly: converge through the normal
        # cancellation path so a terminal is still published exactly once.
        self._signal_cancel(active)

    async def on_child_usage(self, ev: Event) -> None:
        active = self._active.get(ev.session_id)
        if active is None:
            return
        usage = ev.payload.get("usage") or {}
        active.tokens_input += int(usage.get("input_tokens", 0) or 0)
        active.tokens_output += int(usage.get("output_tokens", 0) or 0)

    async def on_child_event(self, ev: Event) -> None:
        """Fold child lifecycle events into throttled ``subagent.progress``."""
        active = self._active.get(ev.session_id)
        if active is None:
            return
        if ev.type in ("tool.call.started", "tool.call.completed", "tool.call.failed"):
            active.last_tool = ev.payload.get("name") or active.last_tool
        now = time.monotonic()
        interval = self.settings.progress_interval_ms / 1000.0
        if interval > 0 and now - active.last_progress < interval:
            return
        active.last_progress = now
        handle = self.host.session(ev.session_id)
        state = handle.agent.state if handle is not None else None
        payload = {
            "call_id": active.call_id,
            "child_session_id": active.child_session_id,
            "title": active.task.title,
            "phase": state.phase if state is not None else "unknown",
            "round": state.round if state is not None else 0,
            "last_tool": active.last_tool,
            "message_count": len(state.messages) if state is not None else 0,
            "tokens_input": active.tokens_input,
            "tokens_output": active.tokens_output,
        }
        await self._publish(active, "subagent.progress", payload)

    # -- lifecycle ----------------------------------------------------------
    def _semaphore(self, parent_session_id: str) -> asyncio.Semaphore:
        semaphore = self._semaphores.get(parent_session_id)
        if semaphore is None:
            semaphore = asyncio.Semaphore(
                max(1, self.settings.max_concurrent_per_session)
            )
            self._semaphores[parent_session_id] = semaphore
        return semaphore

    def _decrement_waiting(self, parent_session_id: str) -> None:
        remaining = self._waiting.get(parent_session_id, 0) - 1
        if remaining > 0:
            self._waiting[parent_session_id] = remaining
        else:
            self._waiting.pop(parent_session_id, None)

    def _release_running(self, parent_session_id: str) -> None:
        remaining = self._running.get(parent_session_id, 0) - 1
        if remaining > 0:
            self._running[parent_session_id] = remaining
        else:
            self._running.pop(parent_session_id, None)

    async def _run_task(self, task: SubagentTask, spawn_ev: Event) -> None:
        parent_id = task.parent_session_id
        acquired = False
        active: Optional[_ActiveChild] = None
        try:
            async with self._semaphore(parent_id):
                acquired = True
                self._running[parent_id] = self._running.get(parent_id, 0) + 1
                self._decrement_waiting(parent_id)
                if task.call_id in self._cancel_requested:
                    self._cancel_requested.discard(task.call_id)
                    raise SubagentError(
                        E_SUBAGENT_CANCELLED,
                        "cancelled before the child was started",
                        context={"parent_session_id": parent_id,
                                 "call_id": task.call_id},
                    )
                await self._execute(task, spawn_ev)
        except asyncio.CancelledError:
            active = self._by_call(task.call_id)
            if active is not None:
                self._close_active(active)
            raise
        except SubagentError as exc:
            await self._fail_spawn(
                task.call_id,
                {"parent_session_id": parent_id, "call_id": task.call_id},
                exc,
                title=task.title,
            )
        except Exception as exc:  # noqa: BLE001 - errors are data (P4)
            await self._fail_spawn(
                task.call_id,
                {"parent_session_id": parent_id, "call_id": task.call_id},
                SubagentError(
                    E_SUBAGENT_FAILED, f"{type(exc).__name__}: {exc}",
                    context={"parent_session_id": parent_id,
                             "call_id": task.call_id},
                ),
                title=task.title,
            )
        finally:
            if acquired:
                self._release_running(parent_id)
            else:
                self._decrement_waiting(parent_id)
            self._task_parent.pop(task.call_id, None)

    async def _execute(self, task: SubagentTask, spawn_ev: Event) -> None:
        policy = task.policy
        assert policy is not None  # validated in on_spawn
        parent = self.host.session(task.parent_session_id)
        if parent is None or parent.kind != "root":
            raise SubagentError(
                E_SUBAGENT_SPAWN, "parent session is no longer open",
                context={"parent_session_id": task.parent_session_id,
                         "call_id": task.call_id},
            )

        child = await self.host.open_subagent_session(
            parent_session_id=task.parent_session_id,
            call_id=task.call_id,
            task_title=task.title,
            trace_id=task.trace_id,
            workspace_root=str(parent.workspace.root),
            tools=policy.tools,
            model_profile=policy.model_profile,
            system_blocks=self._system_blocks(task),
            budget=policy.budget,
            read_only=policy.read_only,
            approval_policy=policy.approval_policy,
            metadata={
                "title": task.title,
                "task": task.task[:200],
                "instructions": task.instructions[:200],
            },
        )
        active = _ActiveChild(
            call_id=task.call_id,
            parent_session_id=task.parent_session_id,
            child_session_id=child.session_id,
            spawn_event_id=task.spawn_event_id,
            trace_id=task.trace_id,
            task=task,
            policy=policy,
            started_at=self.clock(),
        )
        self._active[child.session_id] = active
        try:
            # Cancellation may have arrived while the child was being opened
            # (or the parent may have closed); converge before publishing a
            # started event for a session nobody is waiting on.
            if (
                self._closed
                or task.call_id in self._cancel_requested
                or self.host.session(task.parent_session_id) is None
            ):
                self._cancel_requested.discard(task.call_id)
                await self._cancel_and_close(
                    active, E_SUBAGENT_CANCELLED,
                    "cancelled while the child was starting",
                )
                return
            await self._publish(active, "subagent.started", {
                **policy.started_payload(
                    call_id=task.call_id,
                    child_session_id=child.session_id,
                    parent_session_id=task.parent_session_id,
                ),
                "title": task.title,
            })
            await self.bus.publish(Event.create(
                "user.message",
                {"text": self._user_brief(task)},
                session_id=child.session_id,
                trace_id=task.trace_id,
                cause_id=task.spawn_event_id,
            ))
            try:
                async with asyncio.timeout(policy.timeout_ms / 1000.0):
                    await active.done.wait()
            except TimeoutError:
                await self._cancel_and_close(
                    active, E_SUBAGENT_TIMEOUT,
                    f"subagent exceeded {policy.timeout_ms}ms",
                )
                return

            if active.cancel_requested:
                await self._cancel_and_close(
                    active, E_SUBAGENT_CANCELLED, "subagent cancelled",
                )
                return

            terminal = active.terminal
            if terminal is None:
                self._close_active(active)
                return
            if terminal.type == "system.budget.exceeded":
                await self._cancel_and_close(
                    active, E_SUBAGENT_BUDGET,
                    terminal.payload.get("reason") or "subagent budget exhausted",
                    error_info=terminal.payload.get("error_info"),
                )
                return
            finish = terminal.payload.get("finish")
            if finish == "interrupted":
                # Recovery/close path: the parent's half-round already paired
                # the call; do not synthesise a second terminal (§7.6).
                self._close_active(active)
                return
            if finish == "error":
                await self._finish_failure(
                    active,
                    terminal.payload.get("code") or E_SUBAGENT_FAILED,
                    terminal.payload.get("error") or "subagent failed",
                    error_info=terminal.payload.get("error_info"),
                )
                return
            await self._finish_success(active)
        except SubagentError as exc:
            await self._finish_failure(
                active, exc.code, exc.message, error_info=exc.as_error_info(),
            )
        except asyncio.CancelledError:
            self._close_active(active)
            raise
        except Exception as exc:  # noqa: BLE001 - errors are data (P4)
            await self._finish_failure(
                active, E_SUBAGENT_FAILED,
                f"{type(exc).__name__}: {exc}",
                error_info=error_info(
                    E_SUBAGENT_FAILED,
                    source=ErrorSource.SUBAGENT.value,
                    location=LOCATION_ORCHESTRATOR,
                    message=f"{type(exc).__name__}: {exc}",
                    exc=exc,
                    retryable=True,
                    context={"parent_session_id": task.parent_session_id,
                             "child_session_id": child.session_id,
                             "call_id": task.call_id},
                ).to_dict(),
            )

    # -- terminals ----------------------------------------------------------
    async def _finish_success(self, active: _ActiveChild) -> None:
        if active.terminal_sent:
            return
        handle = self.host.session(active.child_session_id)
        state = handle.agent.state if handle is not None else None
        summary_text = extract_summary(state) if state is not None else EMPTY_SUMMARY
        try:
            summary, summary_ref, truncated = await build_summary(
                summary_text,
                limit=self.settings.max_summary_bytes,
                artifacts=self.artifacts,
                session_id=active.child_session_id,
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - a failed blob write must not hang the parent
            # Degrade exactly like an unavailable store: keep the inline prefix
            # and drop the reference.  The terminal must still be published.
            summary, truncated = truncate_utf8(
                summary_text, self.settings.max_summary_bytes,
            )
            summary_ref = None
        active.terminal_sent = True
        usage = self._usage(active, state)
        rounds = int(state.round) if state is not None else 0
        duration = self._duration_ms(active)
        payload = {
            "call_id": active.call_id,
            "child_session_id": active.child_session_id,
            "title": active.task.title,
            "ok": True,
            "finish": "completed",
            "summary": summary,
            "summary_ref": summary_ref,
            "summary_truncated": truncated,
            "result": {
                "child_session_id": active.child_session_id,
                "status": "completed",
                "rounds": rounds,
                "usage": usage,
                "workspace": ".",
            },
            "usage": usage,
            "rounds": rounds,
            "duration_ms": duration,
            "model_profile": active.policy.model_profile,
        }
        await self._publish(active, "subagent.completed", payload)
        await self._update_status(
            active.child_session_id, "completed", duration,
        )
        self._close_active(active)

    async def _finish_failure(
        self,
        active: _ActiveChild,
        code: str,
        message: str,
        *,
        error_info: Optional[Mapping[str, Any]] = None,
        close: bool = True,
    ) -> None:
        if active.terminal_sent:
            return
        active.terminal_sent = True
        active.status = {
            E_SUBAGENT_CANCELLED: "cancelled",
            E_SUBAGENT_TIMEOUT: "timeout",
        }.get(code, "failed")
        handle = self.host.session(active.child_session_id)
        state = handle.agent.state if handle is not None else None
        info = dict(error_info or {})
        info["code"] = code
        info.setdefault("source", ErrorSource.SUBAGENT.value)
        info["message"] = info.get("message") or message
        info["context"] = {
            **dict(info.get("context") or {}),
            "parent_session_id": active.parent_session_id,
            "child_session_id": active.child_session_id,
            "call_id": active.call_id,
            "phase": state.phase if state is not None else None,
        }
        payload = {
            "call_id": active.call_id,
            "child_session_id": active.child_session_id,
            "title": active.task.title,
            "ok": False,
            "finish": "cancelled" if code == E_SUBAGENT_CANCELLED else
                      "timeout" if code == E_SUBAGENT_TIMEOUT else "error",
            "code": code,
            "error": message,
            "error_info": info,
            "usage": self._usage(active, state),
            "rounds": int(state.round) if state is not None else 0,
            "duration_ms": self._duration_ms(active),
            "model_profile": active.policy.model_profile,
        }
        await self._publish(active, "subagent.failed", payload)
        await self._update_status(active.child_session_id, active.status,
                                  payload["duration_ms"])
        if close:
            self._close_active(active)

    async def _cancel_and_close(
        self,
        active: _ActiveChild,
        code: str,
        message: str,
        *,
        error_info: Optional[Mapping[str, Any]] = None,
    ) -> None:
        """Publish the terminal first, then interrupt and reap the child."""
        await self._finish_failure(
            active, code, message, error_info=error_info, close=False,
        )
        await self._interrupt_child(active)
        await self._wait_idle(active)
        self._close_active(active)

    async def _interrupt_child(self, active: _ActiveChild) -> None:
        if self._closed:
            return
        active.self_interrupt = True
        await self.bus.publish(Event.create(
            "user.interrupt",
            {"reason": "subagent.cancel"},
            session_id=active.child_session_id,
            trace_id=active.trace_id,
            cause_id=active.spawn_event_id,
        ))

    async def _wait_idle(self, active: _ActiveChild) -> None:
        grace = max(0, self.settings.cancel_grace_ms) / 1000.0
        if grace <= 0:
            return
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline:
            handle = self.host.session(active.child_session_id)
            if handle is None:
                return
            state = handle.agent.state
            if state.phase == phases.IDLE and not state.pending:
                return
            await asyncio.sleep(0.01)

    def _close_active(self, active: _ActiveChild) -> None:
        self._active.pop(active.child_session_id, None)
        self._cancel_requested.discard(active.call_id)
        self.host.close_session(active.child_session_id)

    async def _update_status(
        self, child_session_id: str, status: str, duration_ms: int
    ) -> None:
        try:
            info = await self.host.get_session_info(child_session_id)
            if info is None:
                return
            metadata = {
                **info.metadata,
                "subagent_status": status,
                "duration_ms": int(duration_ms),
            }
            from dataclasses import replace
            await self.host.storage.catalog.upsert_session(
                replace(info, metadata=metadata)
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - status is audit metadata
            pass

    # -- helpers ------------------------------------------------------------
    async def _fail_spawn(
        self,
        call_id: str,
        context: Mapping[str, Any],
        exc: SubagentError,
        *,
        title: str = "",
    ) -> None:
        if self._closed:
            return
        parent_id = str(context.get("parent_session_id") or "")
        if not parent_id:
            return
        parent = self.host.session(parent_id)
        trace_id = parent.agent.state.trace_id if parent is not None else None
        info = exc.as_error_info()
        info["context"] = {**context, **dict(info.get("context") or {})}
        await self.bus.publish(Event.create(
            "subagent.failed",
            {
                "call_id": call_id,
                "child_session_id": None,
                "title": title or None,
                "ok": False,
                "finish": "error",
                "code": exc.code,
                "error": exc.message,
                "error_info": info,
                "usage": {"input_tokens": 0, "output_tokens": 0, "usd": 0.0},
                "rounds": 0,
                "duration_ms": 0,
            },
            session_id=parent_id,
            trace_id=trace_id,
        ))

    async def _publish(
        self, active: _ActiveChild, etype: str, payload: dict
    ) -> None:
        if self._closed:
            return
        await self.bus.publish(Event.create(
            etype,
            payload,
            session_id=active.parent_session_id,
            trace_id=active.trace_id,
            cause_id=active.spawn_event_id,
        ))

    def _by_call(self, call_id: str) -> Optional[_ActiveChild]:
        for active in self._active.values():
            if active.call_id == call_id:
                return active
        return None

    @staticmethod
    def _signal_cancel(active: _ActiveChild) -> None:
        active.cancel_requested = True
        active.done.set()

    def _usage(self, active: _ActiveChild, state) -> dict:
        # ``on_child_usage`` is an observer racing the child reducer, so treat
        # the child state's total as authoritative and attribute any remainder
        # to input tokens.  The parent budget then always accounts every child
        # token exactly once.
        total = int(state.tokens_used) if state is not None else 0
        output = active.tokens_output
        input_tokens = max(active.tokens_input, total - output)
        if input_tokens + output < total:
            input_tokens = total - output
        return {
            "input_tokens": input_tokens,
            "output_tokens": output,
            "usd": round(float(state.usd_used), 8) if state is not None else 0.0,
        }

    def _duration_ms(self, active: _ActiveChild) -> int:
        return max(0, int((self.clock() - active.started_at) * 1000.0))

    @staticmethod
    def _system_blocks(task: SubagentTask) -> tuple[str, ...]:
        policy = task.policy
        assert policy is not None
        budget = policy.budget
        budget_bits = []
        if budget.rounds:
            budget_bits.append(f"{budget.rounds} rounds")
        if budget.tokens:
            budget_bits.append(f"{budget.tokens} tokens")
        if budget.usd:
            budget_bits.append(f"${budget.usd:.2f}")
        budget_text = ", ".join(budget_bits) or "host defaults"
        role = (
            f"[Subagent role]\n{task.instructions}"
            if task.instructions.strip()
            else "[Subagent role]\nComplete the delegated task."
        )
        environment = (
            "[Environment]\n"
            "- Workspace: shared with the parent; all paths are relative to "
            "the workspace root.\n"
            f"- Access: {'read-only' if policy.read_only else 'read-write'}\n"
            f"- Available tools: {', '.join(policy.tools) or '(none)'}\n"
            f"- Timeout: {policy.timeout_ms}ms\n"
            f"- Budget: {budget_text}\n"
            "- You cannot spawn subagents."
        )
        contract = (
            "[Output contract]\n"
            f"{task.expected_output or 'Return a concise summary.'}\n"
            "- End with a concise summary. Do not include hidden chain-of-thought."
        )
        return (role, environment, contract)

    @staticmethod
    def _user_brief(task: SubagentTask) -> str:
        return (
            f"[Task]\n{task.task}\n\n"
            f"[Context]\n{task.context or '(none provided)'}\n\n"
            f"[Additional environment notes]\n{task.environment or '(none provided)'}"
        )

    # -- shutdown -----------------------------------------------------------
    def cancel_parent(self, parent_session_id: str) -> None:
        """Signal every task belonging to ``parent_session_id`` (sync)."""
        for active in list(self._active.values()):
            if active.parent_session_id == parent_session_id:
                self._signal_cancel(active)
        for call_id, parent_id in list(self._task_parent.items()):
            if parent_id == parent_session_id:
                self._cancel_requested.add(call_id)

    async def close(self) -> None:
        """Cancel and reap every supervisor task (host shutdown)."""
        if self._closed:
            return
        self._closed = True
        for unsubscribe in self._unsubs:
            unsubscribe()
        self._unsubs.clear()
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for active in list(self._active.values()):
            self.host.close_session(active.child_session_id)
        self._active.clear()
        self._tasks.clear()
        self._task_parent.clear()
        self._cancel_requested.clear()
        self._waiting.clear()
        self._running.clear()


__all__ = ["SubagentOrchestrator"]
