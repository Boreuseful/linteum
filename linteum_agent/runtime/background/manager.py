"""BackgroundTaskManager: the sole subscriber of ``background.*`` (§8).

The manager owns the *lifecycle* of asynchronous work, never the parent's
reducer state:

* validate the host authorization boundary (``agent.background.allow`` plus
  ``ToolSpec.backgroundable``), mint ``task_id`` values and enforce limits;
* run the approval handshake **before** execution (a dangerous command must be
  approved while the foreground step waits);
* publish ``background.started`` first, then the existing
  ``tool.call.requested`` / ``subagent.spawn.requested`` intent with
  ``background: true`` so the single capability subscriber keeps executing;
* translate the underlying terminal into exactly one
  ``background.completed/failed/stopped/lost`` fact;
* stop, cancel and reconcile without ever re-executing a side effect.

``ToolRuntime`` and ``SubagentOrchestrator`` learned only to echo a few payload
markers; all task semantics live here (async ReAct design v0.1 §4.2 / §8.12).
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping, Optional

from ...bus.error_info import ErrorSource, error_info
from ...bus.event import Event, uuid7
from ...config.schema import BackgroundSettings, SubagentSettings
from ..tools.errors import E_DENIED, UnknownTool, ToolUnavailable, ValidationError
from .errors import (
    E_BACKGROUND_ARGS,
    E_BACKGROUND_CANCELLED,
    E_BACKGROUND_DISABLED,
    E_BACKGROUND_LIMIT,
    E_BACKGROUND_LOST,
    E_BACKGROUND_NOT_ALLOWED,
    E_BACKGROUND_NOT_FOUND,
    E_BACKGROUND_NOT_RUNNING,
    E_BACKGROUND_SPAWN,
    E_BACKGROUND_STOPPED,
    E_BACKGROUND_UNKNOWN_TOOL,
    BackgroundError,
)
from .model import (
    PENDING_APPROVAL,
    QUEUED,
    RUNNING,
    SLOT_STATES,
    STARTING,
    STOPPING,
    TERMINAL,
    BackgroundTask,
)

#: Event type for each concrete terminal status.
_TERMINAL_EVENTS = {
    "completed": "background.completed",
    "failed": "background.failed",
    "stopped": "background.stopped",
    "lost": "background.lost",
}


@dataclass(frozen=True)
class _ApprovalSubject:
    """The minimal spec view :class:`ApprovalGate` needs for its preview."""

    name: str
    side_effect: str = "dangerous"


class BackgroundTaskManager:
    """Process-local lifecycle owner for background tool/subagent tasks."""

    def __init__(
        self,
        bus,
        *,
        settings: BackgroundSettings,
        subagent_settings: SubagentSettings,
        registry_for: Callable[[str], Any],
        session_kind: Callable[[str], Optional[str]],
        approvals=None,
        approval_policy: str = "ask",
        approval_policy_for: Optional[Callable[[str], Any]] = None,
        auto_approve: Iterable[str] = (),
        artifacts=None,
        clock: Callable[[], float] = time.time,
        subagent_settings_for: Optional[Callable[[], SubagentSettings]] = None,
    ) -> None:
        self.bus = bus
        self.settings = settings
        self.subagent_settings = subagent_settings
        self._subagent_settings_for = subagent_settings_for
        self._registry_for = registry_for
        self._session_kind = session_kind
        self.approvals = approvals
        self.approval_policy = approval_policy
        self._approval_policy_for = approval_policy_for
        self.auto_approve: frozenset[str] = frozenset(auto_approve)
        self.artifacts = artifacts
        self.clock = clock

        self._tasks: dict[str, BackgroundTask] = {}
        #: ``start event id -> task_id`` for the bus redelivery/idempotency case.
        self._by_request: dict[str, str] = {}
        #: ``start call id -> task_id`` retained until the task terminal.
        self._by_call: dict[str, str] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        #: Sessions with a scheduled cross-session promotion task.
        self._promote_pending: set[str] = set()
        self._closed = False
        self._unsubs: list = []

    # -- subscription -------------------------------------------------------
    def subscribe(self) -> None:
        if self._unsubs or self.bus is None:
            return
        for pattern, handler in (
            ("background.start.requested", self.on_start),
            ("background.stop.requested", self.on_stop),
            ("background.list.requested", self.on_list),
            ("background.cancel.requested", self.on_cancel),
            ("tool.call.completed", self.on_tool_terminal),
            ("tool.call.failed", self.on_tool_terminal),
            ("subagent.completed", self.on_subagent_terminal),
            ("subagent.failed", self.on_subagent_terminal),
            ("tool.cancel.requested", self.on_foreground_cancel),
            ("user.interrupt", self.on_user_interrupt),
        ):
            self._unsubs.append(self.bus.on(pattern, handler))

    def unsubscribe(self) -> None:
        for unsubscribe in self._unsubs:
            unsubscribe()
        self._unsubs.clear()

    def settings_changed(
        self,
        settings: BackgroundSettings,
        subagent_settings: SubagentSettings,
    ) -> None:
        """Refresh settings for new tasks; running tasks keep their snapshot."""
        self.settings = settings
        self.subagent_settings = subagent_settings

    def current_subagent_settings(self) -> SubagentSettings:
        if self._subagent_settings_for is not None:
            return self._subagent_settings_for()
        return self.subagent_settings

    # -- helpers ------------------------------------------------------------
    def _lock(self, session_id: str) -> asyncio.Lock:
        lock = self._locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[session_id] = lock
        return lock

    async def _publish(
        self,
        etype: str,
        payload: dict,
        *,
        session_id: str,
        trace_id: str,
        cause_id: Optional[str] = None,
    ):
        if self._closed:
            return None
        return await self.bus.publish(Event.create(
            etype,
            payload,
            session_id=session_id,
            trace_id=trace_id,
            cause_id=cause_id,
        ))

    async def _resolve_approval_policy(self, session_id: str) -> str:
        provider = self._approval_policy_for
        if provider is None:
            return self.approval_policy
        try:
            chosen = provider(session_id)
            if inspect.isawaitable(chosen):
                chosen = await chosen
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - a broken hook must not hang the start
            return self.approval_policy
        if chosen in ("ask", "allow", "deny"):
            return chosen
        return self.approval_policy

    def _slot_tasks(self, session_id: Optional[str] = None) -> list[BackgroundTask]:
        """Tasks that currently occupy a concurrency slot (queued excluded)."""
        return [
            task for task in self._tasks.values()
            if task.status in SLOT_STATES
            and (session_id is None or task.session_id == session_id)
        ]

    def _has_capacity(
        self, session_id: str, *, exclude_task_id: Optional[str] = None,
    ) -> bool:
        per_session = [
            task for task in self._slot_tasks(session_id)
            if task.task_id != exclude_task_id
        ]
        if len(per_session) >= self.settings.max_running_per_session:
            return False
        total = [
            task for task in self._slot_tasks()
            if task.task_id != exclude_task_id
        ]
        return len(total) < self.settings.max_running_total

    def _check_queue_limit(
        self, session_id: str, *, exclude_task_id: Optional[str] = None,
    ) -> None:
        queued = [
            task for task in self._tasks.values()
            if task.status == QUEUED
            and task.session_id == session_id
            and task.task_id != exclude_task_id
        ]
        if len(queued) >= self.settings.max_queued_per_session:
            raise BackgroundError(
                E_BACKGROUND_LIMIT,
                f"background task queue is full for this session "
                f"({len(queued)}/{self.settings.max_queued_per_session})",
                context={
                    "session_id": session_id,
                    "queued": len(queued),
                    "limit": self.settings.max_queued_per_session,
                },
            )

    def _timeout_for(self, spec) -> float:
        timeout = float(self.settings.default_timeout_s or 0.0)
        if timeout <= 0 and spec is not None:
            timeout = float(spec.timeout)
        if self.settings.max_timeout_s > 0:
            timeout = min(timeout, float(self.settings.max_timeout_s))
        return timeout

    # -- start (§8.3) -------------------------------------------------------
    async def on_start(self, ev: Event) -> None:
        async with self._lock(ev.session_id):
            await self._on_start_locked(ev)

    async def _on_start_locked(self, ev: Event) -> None:
        payload = ev.payload if isinstance(ev.payload, dict) else {}
        call_id = payload.get("call_id")
        if not isinstance(call_id, str) or not call_id:
            return  # malformed core intent: nothing the Agent can pair

        # Bus redelivery (same ev.id): replay the same Started fact, minted as a
        # *new* event because the bus dedups by id (§8.3 step 1).
        prior_id = self._by_request.get(ev.id)
        if prior_id is not None:
            task = self._tasks.get(prior_id)
            if task is not None and task.start_published:
                await self._publish(
                    "background.started", self._started_payload(task),
                    session_id=ev.session_id, trace_id=ev.trace_id,
                    cause_id=ev.id,
                )
            return

        # A provider may reuse a call id in a later round.  A *published* task
        # keeps its own task_id (stoppable independently); an unpublished
        # handshake for the same id is superseded and must not leak a slot.
        existing = self._task_for_call(call_id)
        if (
            existing is not None
            and existing.start_event_id != ev.id
            and not existing.start_published
        ):
            if self.approvals is not None:
                self.approvals.cancel(call_id=call_id)
            self._drop_task(existing)

        # Older queued tasks get first claim on any free slot (including one
        # just released by a superseded handshake).
        await self._promote_queued_locked(ev.session_id)

        try:
            task, spec = await self._prepare_start(ev, call_id, payload)
        except BackgroundError as exc:
            await self._publish(
                "background.start.failed",
                {
                    "call_id": call_id,
                    "tool": payload.get("tool"),
                    **exc.as_payload(),
                },
                session_id=ev.session_id, trace_id=ev.trace_id, cause_id=ev.id,
            )
            return
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - errors are data (P4)
            failure = BackgroundError(
                E_BACKGROUND_SPAWN, f"{type(exc).__name__}: {exc}",
                context={"tool": payload.get("tool")},
            )
            await self._publish(
                "background.start.failed",
                {
                    "call_id": call_id,
                    "tool": payload.get("tool"),
                    **failure.as_payload(),
                },
                session_id=ev.session_id, trace_id=ev.trace_id, cause_id=ev.id,
            )
            return

        self._by_request[ev.id] = task.task_id
        self._tasks[task.task_id] = task
        self._by_call[call_id] = task.task_id

        if task.status == QUEUED:
            await self._queue(task, ev)
            return

        if task.status == PENDING_APPROVAL:
            subject = _ApprovalSubject(
                name=task.name,
                side_effect=(spec.side_effect if spec is not None else "dangerous"),
            )
            if self.approvals is None:
                await self._fail_start(
                    task, call_id, BackgroundError(
                        E_BACKGROUND_APPROVAL,
                        "approval is required but no gate is configured",
                    ), ev,
                )
                return
            try:
                await self.approvals.request(
                    ev, subject,
                    resume=self._resume_start,
                    deny=self._deny_start,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - pair the start call (T1)
                await self._fail_start(
                    task, call_id,
                    BackgroundError(
                        E_BACKGROUND_APPROVAL,
                        f"failed to request approval: {type(exc).__name__}: {exc}",
                    ),
                    ev,
                )
            return

        await self._launch(task, ev, spec=spec)

    async def _prepare_start(
        self, ev: Event, call_id: str, payload: Mapping[str, Any],
        *, exclude_task_id: Optional[str] = None, approved: bool = False,
    ) -> tuple[BackgroundTask, Any]:
        if not self.settings.enabled:
            raise BackgroundError(
                E_BACKGROUND_DISABLED, "background tasks are disabled by the host",
            )
        kind = self._session_kind(ev.session_id)
        if kind != "root":
            raise BackgroundError(
                E_BACKGROUND_NOT_ALLOWED,
                "background tasks are only allowed in root sessions",
                context={"session_id": ev.session_id, "session_kind": kind},
            )

        tool_name = payload.get("tool")
        args = payload.get("args")
        if not isinstance(tool_name, str) or not tool_name.strip():
            raise BackgroundError(
                E_BACKGROUND_ARGS, "start.tool must be a non-empty string",
            )
        if tool_name not in self.settings.allow:
            raise BackgroundError(
                E_BACKGROUND_NOT_ALLOWED,
                f"tool {tool_name!r} is not in agent.background.allow",
                context={"tool": tool_name, "allow": list(self.settings.allow)},
            )
        if not isinstance(args, dict):
            raise BackgroundError(
                E_BACKGROUND_ARGS, "start.args must be an object",
            )

        subagent_settings = self.current_subagent_settings()
        spec = None
        needs_approval = False

        if tool_name == subagent_settings.tool_name:
            task_kind = "subagent"
            if not subagent_settings.enabled:
                raise BackgroundError(
                    E_BACKGROUND_NOT_ALLOWED,
                    f"subagent tool {tool_name!r} is disabled",
                    context={"tool": tool_name},
                )
            policy = subagent_settings.approval_policy
            if policy == "inherit":
                policy = await self._resolve_approval_policy(ev.session_id)
            if policy == "deny":
                raise BackgroundError(
                    E_DENIED, "background subagent spawn is denied by policy",
                )
        else:
            task_kind = "tool"
            try:
                registry = self._registry_for(ev.session_id)
                if inspect.isawaitable(registry):
                    registry = await registry
                spec = registry.spec(tool_name)
            except ToolUnavailable as exc:
                raise BackgroundError(
                    E_BACKGROUND_NOT_ALLOWED,
                    f"tool {tool_name!r} is unavailable",
                    context={"tool": tool_name},
                ) from exc
            except UnknownTool as exc:
                raise BackgroundError(
                    E_BACKGROUND_UNKNOWN_TOOL,
                    f"tool {tool_name!r} is not registered",
                    context={"tool": tool_name},
                ) from exc
            if getattr(spec, "plugin_id", None) is not None:
                raise BackgroundError(
                    E_BACKGROUND_NOT_ALLOWED,
                    f"plugin tool {tool_name!r} cannot run in the background in v0.1",
                    context={"tool": tool_name, "plugin_id": spec.plugin_id},
                )
            if not getattr(spec, "backgroundable", False):
                raise BackgroundError(
                    E_BACKGROUND_NOT_ALLOWED,
                    f"tool {tool_name!r} is not background-capable",
                    context={"tool": tool_name},
                )
            try:
                spec.validate(args)
            except ValidationError as exc:
                raise BackgroundError(
                    E_BACKGROUND_ARGS,
                    f"invalid args for {tool_name!r}: {exc}",
                    context={"tool": tool_name},
                ) from exc

            policy = await self._resolve_approval_policy(ev.session_id)
            if policy == "deny":
                raise BackgroundError(
                    E_DENIED, f"background tool {tool_name!r} is denied by policy",
                )
            needs_approval = (
                bool(spec.requires_approval)
                and policy != "allow"
                and tool_name not in self.auto_approve
            )

        if not self._has_capacity(
            ev.session_id, exclude_task_id=exclude_task_id,
        ):
            # Limits are a waiting room, not a failure: accept the start, give
            # it a task_id, and launch when a slot frees.  Only a *new* start
            # checks the queue cap; an already-admitted task that is being
            # re-checked (approval resume / promotion) just stays queued.
            if exclude_task_id is None:
                self._check_queue_limit(ev.session_id)
            status = QUEUED
        elif needs_approval and not approved:
            status = PENDING_APPROVAL
        else:
            status = STARTING

        return BackgroundTask(
            task_id="bg_" + uuid7(),
            start_call_id=call_id,
            session_id=ev.session_id,
            name=tool_name,
            kind=task_kind,
            args=dict(args),
            trace_id=ev.trace_id,
            start_event_id=ev.id,
            status=status,
            spec_digest=spec.stable_digest() if spec is not None else None,
            start_event=ev,
            approved=approved,
        ), spec

    async def _queue(self, task: BackgroundTask, start_ev: Event) -> None:
        """Accept a start beyond the running limit and wait for a free slot."""
        task.status = QUEUED
        if not task.started_at:
            task.started_at = self.clock()
        await self._publish_started(task, start_ev, status="queued")

    async def _launch(
        self, task: BackgroundTask, start_ev: Event, *, spec=None,
        promote_on_failure: bool = True,
    ) -> None:
        # A queued task already has its start terminal; `_publish_started`
        # emits a running status update instead of a second pairing.
        if not await self._publish_started(
            task, start_ev, status="running",
            promote_on_failure=promote_on_failure,
        ):
            return
        task.status = RUNNING
        try:
            await self._publish_underlying(task, start_ev, spec)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - the task must still terminate
            await self._finish_task(
                task, "failed",
                {
                    "code": E_BACKGROUND_SPAWN,
                    "error": f"{type(exc).__name__}: {exc}",
                    "error_info": error_info(
                        E_BACKGROUND_SPAWN,
                        source=ErrorSource.BACKGROUND.value,
                        message=str(exc),
                        exc=exc,
                        context={"tool": task.name, "task_id": task.task_id},
                    ).to_dict(),
                },
                cause_id=start_ev.id,
                promote=promote_on_failure,
            )

    async def _publish_started(
        self, task: BackgroundTask, start_ev: Event, *, status: str,
        promote_on_failure: bool = True,
    ) -> bool:
        if not task.started_at:
            task.started_at = self.clock()
        task.status = QUEUED if status == "queued" else STARTING
        try:
            committed = await self._publish(
                "background.started", self._started_payload(task, status=status),
                session_id=task.session_id, trace_id=task.trace_id,
                cause_id=start_ev.id,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - pair or terminate the task
            await self._started_publish_failed(
                task, start_ev,
                f"failed to publish background.started: {exc}",
                promote_on_failure,
            )
            return False
        if committed is None:
            await self._started_publish_failed(
                task, start_ev,
                "background.started was not committed",
                promote_on_failure,
            )
            return False
        if not task.start_published:
            task.start_published = True
            task.started_seq = int(getattr(committed, "seq", 0) or 0)
        return True

    async def _started_publish_failed(
        self, task: BackgroundTask, start_ev: Event, message: str,
        promote: bool,
    ) -> None:
        failure = BackgroundError(E_BACKGROUND_SPAWN, message)
        if task.start_published:
            # The queued Started was the start terminal; this is a task failure.
            await self._finish_task(
                task, "failed",
                {
                    "code": failure.code,
                    "error": failure.message,
                    "error_info": failure.as_error_info(),
                },
                cause_id=start_ev.id,
                promote=promote,
            )
            return
        await self._fail_start(
            task, task.start_call_id, failure, start_ev, promote=promote,
        )

    async def _publish_underlying(
        self, task: BackgroundTask, start_ev: Event, spec,
    ) -> None:
        if task.kind == "tool":
            await self.bus.publish(Event.create(
                "tool.call.requested",
                {
                    "call_id": task.task_id,
                    "name": task.name,
                    "args": task.args,
                    "background": True,
                    "approved": True,
                    "timeout_s": self._timeout_for(spec),
                    "start_call_id": task.start_call_id,
                    "spec_digest": task.spec_digest,
                },
                session_id=task.session_id,
                trace_id=task.trace_id,
                cause_id=start_ev.id,
            ))
        else:
            await self.bus.publish(Event.create(
                "subagent.spawn.requested",
                {
                    **task.args,
                    "call_id": task.task_id,
                    "background": True,
                    "start_call_id": task.start_call_id,
                },
                session_id=task.session_id,
                trace_id=task.trace_id,
                cause_id=start_ev.id,
            ))

    async def _fail_start(
        self, task: BackgroundTask, call_id: str,
        failure: BackgroundError, parent: Event, *, promote: bool = True,
    ) -> None:
        """Close a start handshake as a failure.

        Before ``background.started`` the start call is paired with
        ``background.start.failed``.  *After* it (a queued task, or a promotion
        that needs approval), the start terminal already exists, so the task
        lifecycle must end with ``background.failed`` instead — otherwise the
        Agent would ignore the event and the task would stay open forever.
        """
        if task.start_published:
            await self._finish_task(
                task, "failed",
                {
                    "code": failure.code,
                    "error": failure.message,
                    "error_info": failure.as_error_info(),
                },
                cause_id=parent.id,
                promote=promote,
            )
            return
        self._drop_task(task)
        await self._publish(
            "background.start.failed",
            {
                "call_id": call_id,
                "tool": task.name,
                **failure.as_payload(),
            },
            session_id=parent.session_id, trace_id=parent.trace_id,
            cause_id=parent.id,
        )
        if promote:
            await self._promote_queued_locked(parent.session_id)
            self._schedule_other_promotions(parent.session_id)

    @staticmethod
    def _started_payload(
        task: BackgroundTask, *, status: Optional[str] = None,
    ) -> dict:
        if status is None:
            status = "queued" if task.status == QUEUED else "running"
        return {
            "call_id": task.start_call_id,
            "task_id": task.task_id,
            "name": task.name,
            "kind": task.kind,
            "status": status,
            "started_at": task.started_at,
        }

    # -- approval callbacks (§8.4) -----------------------------------------
    async def _resume_start(self, ev: Event) -> None:
        async with self._lock(ev.session_id):
            payload = ev.payload if isinstance(ev.payload, dict) else {}
            call_id = payload.get("call_id")
            task = self._task_for_call(call_id)
            if task is None or task.status != PENDING_APPROVAL:
                return
            try:
                # Approval may have waited a long time: re-check limits and the
                # target implementation before starting (§8.4 step 4).
                task2, spec = await self._prepare_start(
                    ev, call_id, payload, exclude_task_id=task.task_id,
                    approved=True,
                )
            except BackgroundError as exc:
                await self._fail_start(task, call_id, exc, ev)
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - errors are data
                await self._fail_start(
                    task, call_id,
                    BackgroundError(
                        E_BACKGROUND_SPAWN, f"{type(exc).__name__}: {exc}",
                    ),
                    ev,
                )
                return
            # Reuse the existing task id/identity; only refresh the spec digest.
            task.spec_digest = task2.spec_digest
            if task2.kind != task.kind or task2.name != task.name:
                await self._fail_start(
                    task, call_id,
                    BackgroundError(
                        E_BACKGROUND_NOT_ALLOWED,
                        "background target changed while awaiting approval",
                    ),
                    ev,
                )
                return
            task.approved = True
            if task2.status == QUEUED:
                # Approval arrived but every slot is now taken.  If the start
                # call was never paired (approval before launch), pair it now
                # as queued; otherwise keep the existing Started.
                if task.start_published:
                    task.status = QUEUED
                else:
                    await self._queue(task, ev)
                return
            await self._launch(task, ev, spec=spec)

    async def _deny_start(self, ev: Event, reason: str = "approval denied") -> None:
        async with self._lock(ev.session_id):
            payload = ev.payload if isinstance(ev.payload, dict) else {}
            call_id = payload.get("call_id")
            task = self._task_for_call(call_id)
            if task is None or task.status != PENDING_APPROVAL:
                return
            await self._fail_start(
                task, call_id,
                BackgroundError(E_DENIED, reason),
                ev,
            )

    # -- stop / list (§8.6) -------------------------------------------------
    async def on_stop(self, ev: Event) -> None:
        async with self._lock(ev.session_id):
            payload = ev.payload if isinstance(ev.payload, dict) else {}
            call_id = payload.get("call_id")
            task_id = payload.get("task_id")
            task = self._tasks.get(task_id) if isinstance(task_id, str) else None
            if task is None or task.session_id != ev.session_id:
                await self._publish(
                    "background.stop.failed",
                    {
                        "call_id": call_id,
                        "task_id": task_id,
                        "code": E_BACKGROUND_NOT_FOUND,
                        "error": f"task {task_id!r} not found in this session",
                        "error_info": error_info(
                            E_BACKGROUND_NOT_FOUND,
                            source=ErrorSource.BACKGROUND.value,
                            message="task not found",
                            context={"task_id": task_id},
                        ).to_dict(),
                    },
                    session_id=ev.session_id, trace_id=ev.trace_id,
                    cause_id=ev.id,
                )
                return
            if task.is_terminal:
                await self._publish(
                    "background.stop.completed",
                    {
                        "call_id": call_id,
                        "task_id": task.task_id,
                        "status": task.terminal_status,
                        "already_finished": True,
                    },
                    session_id=ev.session_id, trace_id=ev.trace_id,
                    cause_id=ev.id,
                )
                return
            if task.status == STOPPING:
                await self._publish(
                    "background.stop.completed",
                    {
                        "call_id": call_id,
                        "task_id": task.task_id,
                        "status": "stopping",
                        "already_finished": False,
                    },
                    session_id=ev.session_id, trace_id=ev.trace_id,
                    cause_id=ev.id,
                )
                return
            if task.status == PENDING_APPROVAL:
                # No public task_id exists yet; the model cannot legitimately
                # stop it.  Treat it as not found rather than leaking existence.
                await self._publish(
                    "background.stop.failed",
                    {
                        "call_id": call_id,
                        "task_id": task_id,
                        "code": E_BACKGROUND_NOT_FOUND,
                        "error": "task has not started",
                        "error_info": error_info(
                            E_BACKGROUND_NOT_FOUND,
                            source=ErrorSource.BACKGROUND.value,
                            message="task has not started",
                        ).to_dict(),
                    },
                    session_id=ev.session_id, trace_id=ev.trace_id,
                    cause_id=ev.id,
                )
                return
            if task.status == QUEUED:
                # Never launched: pair the stop call and terminal immediately.
                task.stop_requested_by = call_id or "stop"
                task.stop_reason = payload.get("reason")
                await self._publish(
                    "background.stop.completed",
                    {
                        "call_id": call_id,
                        "task_id": task.task_id,
                        "status": "stopping",
                        "already_finished": False,
                    },
                    session_id=ev.session_id, trace_id=ev.trace_id,
                    cause_id=ev.id,
                )
                await self._finish_task(task, "stopped", {}, cause_id=ev.id)
                return

            task.status = STOPPING
            task.stop_requested_by = call_id or "stop"
            task.stop_reason = payload.get("reason")
            await self._publish(
                "background.stop.completed",
                {
                    "call_id": call_id,
                    "task_id": task.task_id,
                    "status": "stopping",
                    "already_finished": False,
                },
                session_id=ev.session_id, trace_id=ev.trace_id,
                cause_id=ev.id,
            )
            await self._request_cancel(task)
            self._arm_stop_grace(task)

    async def on_list(self, ev: Event) -> None:
        async with self._lock(ev.session_id):
            payload = ev.payload if isinstance(ev.payload, dict) else {}
            call_id = payload.get("call_id")
            tasks = [
                self._task_view(task)
                for task in self._tasks.values()
                if task.session_id == ev.session_id and task.start_published
            ]
            tasks.sort(key=lambda item: (item["started_at"], item["task_id"]))
            await self._publish(
                "background.list.completed",
                {"call_id": call_id, "tasks": tasks},
                session_id=ev.session_id, trace_id=ev.trace_id, cause_id=ev.id,
            )

    def _task_view(self, task: BackgroundTask) -> dict:
        if task.is_terminal:
            duration = int((task.terminal or {}).get("duration_ms") or 0)
        elif task.started_at:
            duration = max(0, int((self.clock() - task.started_at) * 1000))
        else:
            duration = 0
        return {
            "task_id": task.task_id,
            "name": task.name,
            "kind": task.kind,
            "status": (task.terminal_status if task.is_terminal else task.status),
            "started_at": task.started_at,
            "duration_ms": duration,
        }

    # -- cancel (§8.4 / §8.8) ----------------------------------------------
    async def on_cancel(self, ev: Event) -> None:
        async with self._lock(ev.session_id):
            payload = ev.payload if isinstance(ev.payload, dict) else {}
            task_ids = []
            if isinstance(payload.get("task_id"), str):
                task_ids.append(payload["task_id"])
            task_ids.extend(
                item for item in (payload.get("task_ids") or ())
                if isinstance(item, str)
            )
            reason = payload.get("reason")
            requested_by = payload.get("requested_by") or "client"
            if requested_by not in ("client", "host", "agent"):
                requested_by = "client"
            for task_id in task_ids:
                task = self._tasks.get(task_id)
                if task is None or task.session_id != ev.session_id:
                    continue
                await self._cancel_task(
                    task, ev, requested_by=requested_by, reason=reason,
                )
            for call_id in payload.get("call_ids") or ():
                if not isinstance(call_id, str):
                    continue
                task = self._task_for_call(call_id)
                if task is None:
                    continue
                await self._cancel_task(
                    task, ev, requested_by=requested_by, reason=reason,
                )

    async def _cancel_task(
        self, task: BackgroundTask, ev: Event, *, requested_by: str,
        reason: Optional[str] = None,
    ) -> None:
        if task.is_terminal:
            return
        if task.status == QUEUED:
            # Never launched: no worker to cancel, close the lifecycle now.
            task.stop_requested_by = requested_by
            task.stop_reason = reason or task.stop_reason
            await self._finish_task(task, "stopped", {}, cause_id=ev.id)
            return
        if not task.start_published:
            if self.approvals is not None:
                self.approvals.cancel(call_id=task.start_call_id)
            await self._fail_start(
                task, task.start_call_id,
                BackgroundError(
                    E_BACKGROUND_CANCELLED,
                    "background start cancelled before it was announced",
                ),
                ev,
            )
            return
        task.status = STOPPING
        task.stop_requested_by = requested_by
        task.stop_reason = reason or task.stop_reason
        await self._request_cancel(task)
        self._arm_stop_grace(task)

    # -- underlying terminals (§8.7) ---------------------------------------
    async def on_tool_terminal(self, ev: Event) -> None:
        payload = ev.payload if isinstance(ev.payload, dict) else {}
        if not payload.get("background"):
            return
        async with self._lock(ev.session_id):
            task = self._tasks.get(payload.get("call_id"))
            if task is None or task.session_id != ev.session_id:
                return
            if task.is_terminal:
                return  # T2: first terminal wins
            if task.status == STOPPING:
                status = "stopped"
            elif ev.type == "tool.call.completed" and payload.get("ok", True):
                status = "completed"
            else:
                status = "failed"
            fields: dict = {}
            if status == "completed":
                fields.update(self._tool_output_fields(payload))
            else:
                fields.update({
                    "code": payload.get("code"),
                    "error": payload.get("error"),
                    "error_info": payload.get("error_info"),
                })
            await self._finish_task(task, status, fields, cause_id=ev.id)

    async def on_subagent_terminal(self, ev: Event) -> None:
        payload = ev.payload if isinstance(ev.payload, dict) else {}
        if not payload.get("background"):
            return
        async with self._lock(ev.session_id):
            task = self._tasks.get(payload.get("call_id"))
            if task is None or task.session_id != ev.session_id:
                return
            if task.is_terminal:
                return
            if task.status == STOPPING:
                status = "stopped"
            elif ev.type == "subagent.completed" and payload.get("ok", True):
                status = "completed"
            else:
                status = "failed"
            fields: dict = {
                "child_session_id": payload.get("child_session_id"),
                "usage": payload.get("usage") or {},
                "rounds": payload.get("rounds"),
            }
            if status == "completed":
                fields.update(self._subagent_output_fields(payload))
            else:
                fields.update({
                    "code": payload.get("code"),
                    "error": payload.get("error"),
                    "error_info": payload.get("error_info"),
                })
                if payload.get("summary") is not None:
                    fields["summary"] = payload.get("summary")
            await self._finish_task(task, status, fields, cause_id=ev.id)

    def _tool_output_fields(self, payload: Mapping[str, Any]) -> dict:
        output = payload.get("output")
        truncated = False
        if isinstance(output, str):
            output, truncated = self._truncate(output)
            if truncated:
                output = f"{output}\n…(truncated by background manager)"
        fields = {
            "output": output,
            "output_ref": payload.get("output_ref"),
            "output_tokens": payload.get("output_tokens"),
        }
        if truncated:
            fields["error_info"] = error_info(
                "E_BACKGROUND_OUTPUT_TRUNCATED",
                source=ErrorSource.BACKGROUND.value,
                message="task output truncated",
                enters_history=False,
                context={"truncated": True},
            ).to_dict()
        return fields

    def _subagent_output_fields(self, payload: Mapping[str, Any]) -> dict:
        summary = payload.get("summary")
        truncated = False
        if isinstance(summary, str):
            summary, truncated = self._truncate(summary)
        fields = {
            "summary": summary,
            "summary_ref": payload.get("summary_ref"),
        }
        if truncated:
            fields["error_info"] = error_info(
                "E_BACKGROUND_OUTPUT_TRUNCATED",
                source=ErrorSource.BACKGROUND.value,
                message="task summary truncated",
                enters_history=False,
                context={"truncated": True},
            ).to_dict()
        return fields

    def _truncate(self, text: str) -> tuple[str, bool]:
        limit = int(self.settings.max_output_bytes or 0)
        if limit <= 0:
            return text, False
        encoded = text.encode("utf-8")
        if len(encoded) <= limit:
            return text, False
        return encoded[:limit].decode("utf-8", "ignore"), True

    # -- terminal arbitration (§8.7 / T2) ----------------------------------
    async def _finish_task(
        self,
        task: BackgroundTask,
        status: str,
        fields: dict,
        *,
        cause_id: Optional[str] = None,
        recovered: bool = False,
        promote: bool = True,
    ) -> bool:
        if task.is_terminal:
            return False
        payload = {
            "task_id": task.task_id,
            "name": task.name,
            "kind": task.kind,
            "status": status,
            "ok": status == "completed",
            **fields,
        }
        if status == "stopped":
            # The lifecycle code is stable regardless of the underlying
            # cancellation signal (E_CANCELLED, timeout, forced grace).
            payload["code"] = E_BACKGROUND_STOPPED
            payload.setdefault("requested_by", task.stop_requested_by)
            if task.stop_reason:
                payload.setdefault("reason", task.stop_reason)
        elif status == "lost":
            payload.setdefault("code", E_BACKGROUND_LOST)
        if status in ("failed", "stopped", "lost"):
            # §16.1: every background failure is unambiguous about its layer.
            # The underlying producer is preserved in ``context``.
            raw = payload.get("error_info")
            if isinstance(raw, dict):
                info = dict(raw)
                info["context"] = {
                    **dict(info.get("context") or {}),
                    "underlying_source": info.get("source"),
                    "underlying_code": info.get("code"),
                    "task_id": task.task_id,
                }
                info["source"] = ErrorSource.BACKGROUND.value
                info["code"] = payload.get("code") or info.get("code")
            else:
                info = error_info(
                    payload.get("code") or E_BACKGROUND_SPAWN,
                    source=ErrorSource.BACKGROUND.value,
                    message=payload.get("error") or status,
                    context={"task_id": task.task_id},
                ).to_dict()
            payload["error_info"] = info
        if "duration_ms" not in payload:
            if recovered:
                payload["duration_ms"] = 0
            else:
                payload["duration_ms"] = max(
                    0, int((self.clock() - task.started_at) * 1000)
                )
        payload["recovered"] = bool(recovered)
        etype = _TERMINAL_EVENTS.get(status, "background.failed")
        # Publish first: if the bus rejects the fact, the task stays
        # non-terminal so a later path (grace timer / duplicate terminal) can
        # still close it exactly once (T2).
        await self._publish(
            etype, payload,
            session_id=task.session_id, trace_id=task.trace_id,
            cause_id=cause_id or task.start_event_id,
        )
        task.status = TERMINAL
        task.terminal = payload
        self._by_call.pop(task.start_call_id, None)
        timer, task.stop_timer = task.stop_timer, None
        if timer is not None and timer is not asyncio.current_task():
            timer.cancel()
        self._prune_terminal_tasks()
        if promote:
            # A slot was freed: let the oldest queued task use it.  Promotion
            # itself may finish tasks, so it runs with promote=False inside.
            await self._promote_queued_locked(task.session_id)
            self._schedule_other_promotions(task.session_id)
        return True

    async def _promote_queued_locked(self, session_id: str) -> None:
        """Launch queued tasks while this session has free slots (FIFO)."""
        if self._closed:
            return
        while True:
            task = next(
                (
                    item for item in self._tasks.values()
                    if item.session_id == session_id and item.status == QUEUED
                ),
                None,
            )
            if task is None:
                return
            if not self._has_capacity(session_id, exclude_task_id=task.task_id):
                return
            if task.start_event is None:
                await self._finish_task(
                    task, "failed",
                    {
                        "code": E_BACKGROUND_LOST,
                        "error": "queued task lost its start event",
                        "error_info": error_info(
                            E_BACKGROUND_LOST,
                            source=ErrorSource.BACKGROUND.value,
                            message="queued task lost its start event",
                            context={"task_id": task.task_id},
                        ).to_dict(),
                    },
                    cause_id=task.start_event_id, promote=False,
                )
                continue

            payload = (
                task.start_event.payload
                if isinstance(task.start_event.payload, dict) else {}
            )
            try:
                prepared, spec = await self._prepare_start(
                    task.start_event, task.start_call_id, payload,
                    exclude_task_id=task.task_id, approved=task.approved,
                )
            except BackgroundError as exc:
                await self._finish_task(
                    task, "failed",
                    {
                        "code": exc.code,
                        "error": exc.message,
                        "error_info": exc.as_error_info(),
                    },
                    cause_id=task.start_event_id, promote=False,
                )
                continue
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - errors are data
                failure = BackgroundError(
                    E_BACKGROUND_SPAWN, f"{type(exc).__name__}: {exc}",
                )
                await self._finish_task(
                    task, "failed",
                    {
                        "code": failure.code,
                        "error": failure.message,
                        "error_info": failure.as_error_info(),
                    },
                    cause_id=task.start_event_id, promote=False,
                )
                continue

            task.spec_digest = prepared.spec_digest
            if prepared.kind != task.kind or prepared.name != task.name:
                await self._finish_task(
                    task, "failed",
                    {
                        "code": E_BACKGROUND_NOT_ALLOWED,
                        "error": "background target changed while queued",
                        "error_info": BackgroundError(
                            E_BACKGROUND_NOT_ALLOWED,
                            "background target changed while queued",
                        ).as_error_info(),
                    },
                    cause_id=task.start_event_id, promote=False,
                )
                continue
            if prepared.status == QUEUED:
                return  # capacity changed; keep waiting
            if prepared.status == PENDING_APPROVAL:
                task.status = PENDING_APPROVAL
                subject = _ApprovalSubject(
                    name=task.name,
                    side_effect=(
                        spec.side_effect if spec is not None else "dangerous"
                    ),
                )
                if self.approvals is None:
                    await self._finish_task(
                        task, "failed",
                        {
                            "code": E_BACKGROUND_APPROVAL,
                            "error": "approval is required but no gate is configured",
                            "error_info": BackgroundError(
                                E_BACKGROUND_APPROVAL,
                                "approval is required but no gate is configured",
                            ).as_error_info(),
                        },
                        cause_id=task.start_event_id, promote=False,
                    )
                    continue
                try:
                    await self.approvals.request(
                        task.start_event, subject,
                        resume=self._resume_start, deny=self._deny_start,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - terminate cleanly
                    await self._finish_task(
                        task, "failed",
                        {
                            "code": E_BACKGROUND_APPROVAL,
                            "error": (
                                "failed to request approval: "
                                f"{type(exc).__name__}: {exc}"
                            ),
                            "error_info": BackgroundError(
                                E_BACKGROUND_APPROVAL,
                                "failed to request approval",
                            ).as_error_info(),
                        },
                        cause_id=task.start_event_id, promote=False,
                    )
                    continue
                return  # approval owns the slot until resolved

            await self._launch(
                task, task.start_event, spec=spec, promote_on_failure=False,
            )
            # A successful launch occupies the slot; the loop head re-checks
            # capacity.  A failed launch is terminal and the loop continues.

    def _schedule_other_promotions(self, session_id: str) -> None:
        """Best-effort promotion for sessions blocked by the process-wide cap."""
        if self._closed:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        others = {
            task.session_id for task in self._tasks.values()
            if task.status == QUEUED and task.session_id != session_id
        }
        for other in others:
            if other in self._promote_pending:
                continue
            self._promote_pending.add(other)
            loop.create_task(self._promote_session(other))

    async def _promote_session(self, session_id: str) -> None:
        try:
            async with self._lock(session_id):
                await self._promote_queued_locked(session_id)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - promotion is best effort
            pass
        finally:
            self._promote_pending.discard(session_id)

    def _prune_terminal_tasks(self) -> None:
        keep = max(1, int(self.settings.max_terminal_tasks or 1))
        terminal = [
            task for task in self._tasks.values() if task.is_terminal
        ]
        overflow = len(terminal) - keep
        if overflow <= 0:
            return
        for task in sorted(terminal, key=lambda item: (item.started_at, item.task_id))[:overflow]:
            self._tasks.pop(task.task_id, None)
            for event_id, task_id in list(self._by_request.items()):
                if task_id == task.task_id:
                    self._by_request.pop(event_id, None)

    def _drop_task(self, task: BackgroundTask) -> None:
        self._tasks.pop(task.task_id, None)
        self._by_call.pop(task.start_call_id, None)
        for event_id, task_id in list(self._by_request.items()):
            if task_id == task.task_id:
                self._by_request.pop(event_id, None)
        if task.stop_timer is not None:
            task.stop_timer.cancel()
            task.stop_timer = None

    # -- stop grace (§8.6) --------------------------------------------------
    def _arm_stop_grace(self, task: BackgroundTask) -> None:
        if task.stop_timer is not None:
            return
        grace = max(0, int(self.settings.stop_grace_ms or 0)) / 1000.0

        async def _force() -> None:
            try:
                await asyncio.sleep(grace)
                async with self._lock(task.session_id):
                    if task.is_terminal or task.status != STOPPING:
                        return
                    # Re-issue the idempotent cancel, then surface the terminal
                    # even if the worker never answered (§8.6 step 4).
                    with contextlib.suppress(Exception):
                        await self._request_cancel(task)
                    await self._finish_task(
                        task, "stopped",
                        {
                            "code": "E_BACKGROUND_STOPPED",
                            "requested_by": task.stop_requested_by,
                            "forced": True,
                        },
                        cause_id=task.start_event_id,
                    )
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - best effort timer
                pass

        task.stop_timer = asyncio.get_running_loop().create_task(_force())

    async def _request_cancel(self, task: BackgroundTask) -> None:
        if task.status == QUEUED:
            return  # never launched: nothing to cancel
        if task.kind == "tool":
            await self.bus.publish(Event.create(
                "tool.cancel.requested",
                {"call_id": task.task_id},
                session_id=task.session_id,
                trace_id=task.trace_id,
                cause_id=task.start_event_id,
            ))
        else:
            await self.bus.publish(Event.create(
                "subagent.cancel.requested",
                {"call_ids": [task.task_id]},
                session_id=task.session_id,
                trace_id=task.trace_id,
                cause_id=task.start_event_id,
            ))

    # -- interruption helpers ----------------------------------------------
    async def on_foreground_cancel(self, ev: Event) -> None:
        payload = ev.payload if isinstance(ev.payload, dict) else {}
        call_id = payload.get("call_id")
        scope = payload.get("scope") or "foreground"
        async with self._lock(ev.session_id):
            if call_id:
                task = self._task_for_call(call_id)
                if task is not None and not task.start_published:
                    await self._cancel_task(task, ev, requested_by="agent")
                return
            if scope == "background":
                return
            await self._cancel_pending_starts_locked(
                ev.session_id, parent=ev,
            )

    async def on_user_interrupt(self, ev: Event) -> None:
        async with self._lock(ev.session_id):
            await self._cancel_pending_starts_locked(ev.session_id, parent=ev)

    async def _cancel_pending_starts_locked(
        self, session_id: str, *, parent: Event,
    ) -> None:
        """Close every start handshake that never produced a Started (§8.4)."""
        for task in list(self._tasks.values()):
            if task.session_id != session_id or task.start_published:
                continue
            if task.status not in (PENDING_APPROVAL, STARTING):
                continue
            if self.approvals is not None:
                self.approvals.cancel(call_id=task.start_call_id)
            await self._fail_start(
                task, task.start_call_id,
                BackgroundError(
                    E_BACKGROUND_CANCELLED,
                    "background start cancelled before it was announced",
                ),
                parent,
            )

    def drop_pending_starts(self, session_id: str) -> None:
        """Synchronous, best-effort hook for ``Host.close_session`` (§8.4).

        It never blocks the caller.  A running event loop receives a scheduled
        cleanup; without one the in-memory pending starts are simply dropped.
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            for task in list(self._tasks.values()):
                if task.session_id == session_id and not task.start_published:
                    self._drop_task(task)
            return
        loop.create_task(self._drop_pending_starts_async(session_id))

    async def _drop_pending_starts_async(self, session_id: str) -> None:
        async with self._lock(session_id):
            parent = Event.create(
                "user.interrupt",
                {"reason": "approvals cancelled", "internal": True},
                session_id=session_id,
            )
            if self._session_kind(session_id) is None:
                parent = None
            for task in list(self._tasks.values()):
                if task.session_id != session_id or task.start_published:
                    continue
                if task.status not in (PENDING_APPROVAL, STARTING):
                    continue
                if self.approvals is not None:
                    self.approvals.cancel(call_id=task.start_call_id)
                if parent is not None:
                    await self._fail_start(
                        task, task.start_call_id,
                        BackgroundError(
                            E_BACKGROUND_CANCELLED,
                            "background start cancelled before it was announced",
                        ),
                        parent,
                    )
                else:
                    self._drop_task(task)

    # -- recovery (§8.10 / §12.3) ------------------------------------------
    async def recover(
        self,
        session_id: str,
        events: Iterable[Event],
        *,
        pending_start_ids: Iterable[str] = (),
    ) -> None:
        """Reconcile a reopened session against its full hydrated WAL.

        Never executes anything: tasks with an underlying terminal are reported
        with the recorded result; tasks without one become ``lost``.  All
        recovery terminals carry ``recovered: true`` so the reducer never
        auto-continues (T6).
        """
        async with self._lock(session_id):
            pending_ids = set(pending_start_ids)
            starts: dict[str, Mapping[str, Any]] = {}
            start_events: dict[str, Event] = {}
            started: dict[str, Event] = {}
            start_terminal: set[str] = set()
            task_terminals: dict[str, Mapping[str, Any]] = {}
            underlying: dict[str, Event] = {}
            stopping: set[str] = set()
            fallback_trace = ""

            for ev in events:
                if ev.session_id != session_id:
                    continue
                fallback_trace = fallback_trace or ev.trace_id
                payload = ev.payload if isinstance(ev.payload, dict) else {}
                call_id = payload.get("call_id")
                if ev.type == "background.start.requested" and call_id:
                    starts[call_id] = payload
                    start_events[call_id] = ev
                elif ev.type == "background.started" and call_id:
                    started[call_id] = ev
                elif ev.type == "background.start.failed" and call_id:
                    start_terminal.add(call_id)
                elif ev.type in (
                    "background.completed", "background.failed",
                    "background.stopped", "background.lost",
                ) and payload.get("task_id"):
                    task_terminals[payload["task_id"]] = payload
                elif ev.type in (
                    "tool.call.completed", "tool.call.failed",
                    "subagent.completed", "subagent.failed",
                ) and payload.get("background") and call_id:
                    underlying[call_id] = ev
                elif ev.type == "background.stop.requested" and payload.get("task_id"):
                    stopping.add(payload["task_id"])

            # 1) Register already-terminal tasks so a second recover is a no-op.
            for task_id, payload in task_terminals.items():
                if task_id in self._tasks:
                    continue
                self._tasks[task_id] = BackgroundTask(
                    task_id=task_id,
                    start_call_id="",
                    session_id=session_id,
                    name=str(payload.get("name") or ""),
                    kind=str(payload.get("kind") or "tool"),
                    args={},
                    trace_id=fallback_trace,
                    start_event_id="",
                    status=TERMINAL,
                    terminal=dict(payload),
                )

            # 2) Tasks that were announced: translate a recorded underlying
            #    terminal, otherwise report ``lost`` (never re-execute).
            for call_id, started_ev in started.items():
                payload = started_ev.payload if isinstance(started_ev.payload, dict) else {}
                task_id = payload.get("task_id")
                if not isinstance(task_id, str) or not task_id:
                    continue
                if task_id in task_terminals:
                    continue
                task = self._tasks.get(task_id)
                if task is None:
                    start_ev = start_events.get(call_id)
                    start_payload = starts.get(call_id, {})
                    task = BackgroundTask(
                        task_id=task_id,
                        start_call_id=call_id,
                        session_id=session_id,
                        name=str(payload.get("name") or ""),
                        kind=str(payload.get("kind") or "tool"),
                        args=dict(start_payload.get("args") or {})
                        if isinstance(start_payload.get("args"), dict) else {},
                        trace_id=started_ev.trace_id,
                        start_event_id=(start_ev.id if start_ev else started_ev.id),
                        status=STOPPING if task_id in stopping else RUNNING,
                        started_at=float(started_ev.ts),
                        started_seq=int(started_ev.seq or 0),
                        start_published=True,
                        start_event=start_ev,
                    )
                    self._tasks[task_id] = task
                    self._by_call[call_id] = task_id
                if task.is_terminal:
                    continue
                # Underlying calls are keyed by the task id (the manager's
                # ``call_id`` for ``tool.call.*`` / ``subagent.*``), not by the
                # model's original start call id (§6.5).
                terminal_ev = underlying.get(task_id)
                if terminal_ev is None:
                    await self._finish_task(
                        task, "lost", {"reason": "process restart"},
                        cause_id=started_ev.id, recovered=True,
                    )
                    continue
                await self._recover_terminal(task, terminal_ev, started_ev)

            # 3) Start handshakes without a Started: keep an approval that the
            #    gate restored, otherwise close the call as lost/cancelled.
            for call_id in starts:
                if call_id in started or call_id in start_terminal:
                    continue
                if call_id in pending_ids and self.approvals is not None:
                    entry = self.approvals.pending.get(call_id)
                    if entry is not None:
                        entry.resume = self._resume_start
                        entry.deny = self._deny_start
                        self._register_pending_start(
                            session_id, call_id, starts[call_id], start_events[call_id],
                        )
                        continue
                await self._publish(
                    "background.start.failed",
                    {
                        "call_id": call_id,
                        "tool": starts[call_id].get("tool"),
                        "code": E_BACKGROUND_LOST,
                        "error": "background start lost after process restart",
                        "error_info": error_info(
                            E_BACKGROUND_LOST,
                            source=ErrorSource.BACKGROUND.value,
                            message="background start lost after process restart",
                            context={"call_id": call_id, "recovered": True},
                        ).to_dict(),
                        "recovered": True,
                    },
                    session_id=session_id,
                    trace_id=start_events[call_id].trace_id,
                    cause_id=start_events[call_id].id,
                )

    def _register_pending_start(
        self,
        session_id: str,
        call_id: str,
        payload: Mapping[str, Any],
        start_ev: Event,
    ) -> BackgroundTask:
        existing = self._task_for_call(call_id)
        if existing is not None:
            return existing
        task_id = "bg_" + uuid7()
        tool = payload.get("tool")
        subagent_name = self.current_subagent_settings().tool_name
        kind = "subagent" if tool == subagent_name else "tool"
        task = BackgroundTask(
            task_id=task_id,
            start_call_id=call_id,
            session_id=session_id,
            name=str(tool or ""),
            kind=kind,
            args=dict(payload.get("args") or {})
            if isinstance(payload.get("args"), dict) else {},
            trace_id=start_ev.trace_id,
            start_event_id=start_ev.id,
            status=PENDING_APPROVAL,
            start_event=start_ev,
        )
        self._tasks[task_id] = task
        self._by_call[call_id] = task_id
        return task

    async def _recover_terminal(
        self, task: BackgroundTask, terminal_ev: Event, started_ev: Event,
    ) -> None:
        payload = terminal_ev.payload if isinstance(terminal_ev.payload, dict) else {}
        if task.status == STOPPING:
            status = "stopped"
        elif terminal_ev.type in (
            "tool.call.completed", "subagent.completed",
        ) and payload.get("ok", True):
            status = "completed"
        else:
            status = "failed"
        fields: dict = {}
        if terminal_ev.type.startswith("tool."):
            if status == "completed":
                fields.update(self._tool_output_fields(payload))
            else:
                fields.update({
                    "code": payload.get("code"),
                    "error": payload.get("error"),
                    "error_info": payload.get("error_info"),
                })
        else:
            fields.update({
                "child_session_id": payload.get("child_session_id"),
                "usage": payload.get("usage") or {},
            })
            if status == "completed":
                fields.update(self._subagent_output_fields(payload))
            else:
                fields.update({
                    "code": payload.get("code"),
                    "error": payload.get("error"),
                    "error_info": payload.get("error_info"),
                    "summary": payload.get("summary"),
                })
        duration = max(0, int((float(terminal_ev.ts) - float(started_ev.ts)) * 1000))
        fields["duration_ms"] = duration
        await self._finish_task(
            task, status, fields, cause_id=terminal_ev.id, recovered=True,
        )

    # -- shutdown (§8.11) ---------------------------------------------------
    def cancel_session(self, session_id: str) -> None:
        """Synchronous best-effort teardown for ``Host.close_session``.

        Runs on the event-loop thread without awaiting, so it is atomic relative
        to async handlers.  Terminals are scheduled best-effort, never awaited.
        """
        if self.approvals is not None:
            with contextlib.suppress(Exception):
                self.approvals.cancel(session_id=session_id)
        for task in list(self._tasks.values()):
            if task.session_id != session_id:
                continue
            if task.is_terminal:
                self._drop_task(task)
                continue
            was_started = task.start_published
            if was_started:
                # Best-effort real teardown: the worker kills the process /
                # cancels the child; the terminal below is the lifecycle fact.
                self._schedule(self._best_effort_request_cancel(task))
                terminal = {
                    "task_id": task.task_id,
                    "name": task.name,
                    "kind": task.kind,
                    "status": "stopped",
                    "ok": False,
                    "code": "E_BACKGROUND_STOPPED",
                    "requested_by": "host",
                    "reason": "session closed",
                    "duration_ms": max(
                        0, int((self.clock() - task.started_at) * 1000)
                    ),
                    "error_info": error_info(
                        E_BACKGROUND_STOPPED,
                        source=ErrorSource.BACKGROUND.value,
                        message="session closed",
                        enters_history=False,
                        context={"task_id": task.task_id},
                    ).to_dict(),
                    "recovered": False,
                }
                task.status = TERMINAL
                task.terminal = terminal
                self._schedule_terminal(task, "background.stopped", terminal)
            else:
                self._schedule_start_failure(task, E_BACKGROUND_CANCELLED,
                                             "session closed")
            self._drop_task(task)
        # Keep the lock object: a handler may still be waiting on it, and a new
        # lock under the same key would let it interleave with that waiter.

    def _schedule_terminal(self, task: BackgroundTask, etype: str, payload: dict) -> None:
        self._schedule(self._best_effort_publish(
            etype, payload, task.session_id, task.trace_id, task.start_event_id,
        ))

    def _schedule_start_failure(
        self, task: BackgroundTask, code: str, message: str,
    ) -> None:
        payload = {
            "call_id": task.start_call_id,
            "tool": task.name,
            "code": code,
            "error": message,
            "error_info": error_info(
                code,
                source=ErrorSource.BACKGROUND.value,
                message=message,
                context={"task_id": task.task_id},
            ).to_dict(),
        }
        self._schedule(self._best_effort_publish(
            "background.start.failed", payload, task.session_id,
            task.trace_id, task.start_event_id,
        ))

    @staticmethod
    def _schedule(coro) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            coro.close()
            return
        loop.create_task(coro)

    async def _best_effort_request_cancel(self, task: BackgroundTask) -> None:
        try:
            await self._request_cancel(task)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - shutdown is best effort (§8.11)
            pass

    async def _best_effort_publish(
        self, etype: str, payload: dict, session_id: str,
        trace_id: str, cause_id: Optional[str],
    ) -> None:
        try:
            await self.bus.publish(Event.create(
                etype, payload, session_id=session_id,
                trace_id=trace_id, cause_id=cause_id,
            ))
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - shutdown is best effort (§8.11)
            pass

    async def close(self) -> None:
        """Cancel every remaining task and publish its terminal (§8.11)."""
        if self._closed:
            return
        self._closed = True
        self.unsubscribe()
        remaining = [
            task for task in self._tasks.values() if not task.is_terminal
        ]
        # Give workers one last chance to honour the cancellation before the
        # lifecycle fact is written (bounded by ``stop_grace_ms``).
        for task in remaining:
            if task.start_published:
                await self._best_effort_request_cancel(task)
        grace = max(0, int(self.settings.stop_grace_ms or 0)) / 1000.0
        if remaining and grace > 0:
            await asyncio.sleep(grace)
        for task in remaining:
            if task.is_terminal:
                continue
            if task.start_published:
                terminal = {
                    "task_id": task.task_id,
                    "name": task.name,
                    "kind": task.kind,
                    "status": "stopped",
                    "ok": False,
                    "code": "E_BACKGROUND_STOPPED",
                    "requested_by": "host",
                    "reason": "host shutdown",
                    "duration_ms": max(
                        0, int((self.clock() - task.started_at) * 1000)
                    ),
                    "error_info": error_info(
                        E_BACKGROUND_STOPPED,
                        source=ErrorSource.BACKGROUND.value,
                        message="host shutdown",
                        enters_history=False,
                        context={"task_id": task.task_id},
                    ).to_dict(),
                    "recovered": False,
                }
                task.status = TERMINAL
                task.terminal = terminal
                await self._best_effort_publish(
                    "background.stopped", terminal, task.session_id,
                    task.trace_id, task.start_event_id,
                )
            else:
                task.status = TERMINAL
                await self._best_effort_publish(
                    "background.start.failed",
                    {
                        "call_id": task.start_call_id,
                        "tool": task.name,
                        "code": E_BACKGROUND_CANCELLED,
                        "error": "host shut down",
                        "error_info": error_info(
                            E_BACKGROUND_CANCELLED,
                            source=ErrorSource.BACKGROUND.value,
                            message="host shut down",
                        ).to_dict(),
                    },
                    task.session_id, task.trace_id, task.start_event_id,
                )
            timer, task.stop_timer = task.stop_timer, None
            if timer is not None:
                timer.cancel()
        self._tasks.clear()
        self._by_request.clear()
        self._by_call.clear()
        self._promote_pending.clear()
        self._locks.clear()

    # -- accessors ----------------------------------------------------------
    def _task_for_call(self, call_id: Optional[str]) -> Optional[BackgroundTask]:
        if not call_id:
            return None
        task_id = self._by_call.get(call_id)
        if task_id is None:
            return None
        return self._tasks.get(task_id)

    def task(self, task_id: str) -> Optional[BackgroundTask]:
        return self._tasks.get(task_id)

    def tasks(self, session_id: Optional[str] = None) -> list[BackgroundTask]:
        return [
            task for task in self._tasks.values()
            if session_id is None or task.session_id == session_id
        ]

    def __len__(self) -> int:
        return len(self._tasks)


__all__ = ["BackgroundTaskManager"]
