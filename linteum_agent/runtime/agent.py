"""The Agent: a pure, event-driven reducer (§19).

The agent owns **no I/O**.  Every method here is a
``reduce(state, event) -> (new_state, [intent events])`` step: it appends final
results to ``messages`` (never ``delta`` — I4), maintains the ``pending`` barrier
(§18.2), advances the phase machine (§17) and publishes *intents* (``llm.request``
/ ``tool.call.requested`` / ``agent.completed``) for the effect layers to act on.

Two structural properties fall out of this shape:

* **P1/P11** — no ``await`` on a provider, no clock, no registry lookup: the
  reducer is replayable and the context is a derived view built on demand.
* **§18.2 barrier** — tools fan out concurrently but the loop only advances once
  ``pending`` is empty, which is how "session serial / tools parallel" is enacted.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from typing import Optional, Sequence

from ..bus.artifact import NullArtifactStore, hydrate_events
from ..bus.errors import BusError
from ..bus.event import INLINE_LIMIT, Event, uuid7
from .context import ContextBuilder, ContextPolicy, PassthroughContextBuilder, normalise_message
from .errors import ErrorSource, error_info
from .state import AgentState, Budget, phases
from .state.checkpoint import Checkpointer

#: Marker appended to a message whose round the user interrupted (§16.3).  It is
#: written into ``content`` (so it survives into the provider context) and paired
#: with an ``interrupted`` flag host UIs can read.
INTERRUPT_TAG = "[interrupted by user]"

#: Sentinel for ``rebind`` arguments whose ``None`` is a meaningful value.
_UNSET = object()


class AgentBusy(RuntimeError):
    """The reducer surface cannot be replaced in the current phase."""

    def __init__(self, message: str = "agent is busy", *, phase=None) -> None:
        super().__init__(message)
        self.phase = phase


def _tag_interrupt(content) -> str:
    """Return ``content`` as text with :data:`INTERRUPT_TAG` appended.

    Accepts either a plain string or the canonical ``content`` part list and
    never leaves the tag off (an empty partial still yields the bare tag).
    """
    if isinstance(content, list):
        text = "".join(part.get("text", "") for part in content
                       if isinstance(part, dict) and part.get("type") == "text")
    else:
        text = content or ""
    text = (text or "").rstrip()
    return f"{text}\n\n{INTERRUPT_TAG}" if text else INTERRUPT_TAG


class Agent:
    def __init__(
        self,
        bus,
        *,
        session_id: str,
        model: str = "default",
        schemas=None,
        tools_schema=(),
        checkpointer: Optional[Checkpointer] = None,
        budget: Optional[Budget] = None,
        ctx_builder: Optional[ContextBuilder] = None,
        context_policy: Optional[ContextPolicy] = None,
        subagent_tools: frozenset[str] = frozenset(),
        background_tools: frozenset[str] = frozenset(),
        auto_continue: bool = True,
        cancel_on_interrupt: bool = False,
        auto_subscribe: bool = True,
        replay: bool = False,
        serialize: bool = True,
    ) -> None:
        self.bus = bus
        self.session_id = session_id
        self.model = model
        #: Reserved tool names the reducer routes to ``subagent.spawn.requested``
        #: instead of ``tool.call.requested`` (subagent design §3.1 / §6.1).
        #: Empty for child sessions, which is how ``max_depth=1`` is enforced.
        self._subagent_tools = frozenset(subagent_tools)
        #: Reserved names routed to ``background.*`` intents (``start`` / ``stop``
        #: / ``list_tasks``).  Empty for child sessions and when the feature is
        #: disabled, preserving today's behaviour exactly (D10/D11).
        self._background_tools = frozenset(background_tools)
        #: ``agent.background.auto_continue`` — whether an idle session opens a
        #: new step when a completed/failed task terminal arrives (D8).
        self._auto_continue = bool(auto_continue)
        #: ``agent.background.cancel_on_interrupt`` — whether a user interrupt
        #: also cancels already-running background tasks (§7.8).
        self._cancel_on_interrupt = bool(cancel_on_interrupt)
        self.state = AgentState(session_id=session_id, trace_id=uuid7(), budget=budget or Budget())
        self.checkpointer = checkpointer
        self.ctx_builder = ctx_builder or PassthroughContextBuilder()
        self.context_policy = context_policy
        self._replaying = replay
        #: Serialise this session's reducer handlers (``WorkerPool`` permits up to
        #: four concurrent handlers per session; the reducer itself is not
        #: re-entrant).  A host that already provides its own lane can opt out.
        self._serialize = serialize
        self._reducer_lock = asyncio.Lock()
        #: Live subscriptions; ``close()`` removes them (needed for session
        #: eviction / host shutdown — the bus otherwise keeps the agent alive).
        self._unsubs: list = []
        if context_policy is not None and context_policy.enabled \
                and not hasattr(self.ctx_builder, "next_compaction"):
            raise ValueError(
                "context_policy requires a ContextBuilder with next_compaction()"
            )
        self._schemas_provider = schemas          # callable -> list[dict] (ToolRegistry.schemas)
        self._static_schemas = tuple(tools_schema)
        if auto_subscribe:
            self.subscribe()

    # -- subscription -------------------------------------------------------
    def subscribe(self) -> None:
        """Attach the reducer handlers to the bus (idempotent).

        Every handler is wrapped in the per-session reducer lock.  The bus
        deliberately allows several handlers per session to run concurrently
        (§6.2 / ``bus/scheduler/pool.py``), which is fine for effect layers but
        would let two reducer events interleave on the same ``AgentState``.
        """
        if self._unsubs or self.bus is None:
            return
        bus = self.bus
        for pattern, handler in (
            ("user.message", self.on_user),
            ("user.interrupt", self.on_interrupt),
            ("llm.completed", self.on_llm),
            ("llm.failed", self.on_llm_failed),
            ("tool.call.completed", self.on_tool_done),
            ("tool.call.failed", self.on_tool_done),
            ("subagent.completed", self.on_subagent_done),
            ("subagent.failed", self.on_subagent_done),
            ("subagent.started", self.on_subagent_notice),
            ("subagent.progress", self.on_subagent_notice),
            ("approval.granted", self.on_approval),
            ("agent.resumed", self.on_resumed),
            ("context.compaction.applied", self.on_compaction_applied),
            ("system.plugin.loaded", self.on_tools_changed),
            ("system.plugin.unloaded", self.on_tools_changed),
            # async ReAct: the Agent pairs start/stop/list calls and reduces
            # task terminals into notifications (§7.1).
            ("background.started", self.on_background_started),
            ("background.start.failed", self.on_background_start_failed),
            ("background.stop.completed", self.on_background_stop_done),
            ("background.stop.failed", self.on_background_stop_done),
            ("background.list.completed", self.on_background_list_done),
            ("background.completed", self.on_background_terminal),
            ("background.failed", self.on_background_terminal),
            ("background.stopped", self.on_background_terminal),
            ("background.lost", self.on_background_terminal),
        ):
            self._unsubs.append(bus.on(pattern, self._serialized(handler)))

    def _serialized(self, handler):
        """Wrap ``handler`` so reducer events for this session never overlap."""
        if not self._serialize:
            return handler

        async def serialized(ev: Event) -> None:
            async with self._reducer_lock:
                await handler(ev)

        serialized.__name__ = getattr(handler, "__name__", "handler")
        serialized.__qualname__ = f"Agent.{serialized.__name__}"
        return serialized

    def unsubscribe(self) -> None:
        """Detach every handler (idempotent); the agent becomes inert."""
        for unsubscribe in self._unsubs:
            unsubscribe()
        self._unsubs.clear()

    async def rebind(
        self,
        *,
        ctx_builder: Optional[ContextBuilder] = None,
        context_policy=_UNSET,
        schemas=_UNSET,
        subagent_tools: Optional[frozenset[str]] = None,
        background_tools: Optional[frozenset[str]] = None,
        commit=None,
    ) -> None:
        """Replace the host-owned surface under the reducer lock (§10.2).

        Only ``idle`` sessions with no pending call, approval, compaction or
        interrupt may be rebound.  ``commit`` runs *before* the swap so a
        metadata/registry failure leaves the old surface intact.
        """
        async with self._reducer_lock:
            if (
                self.state.phase != phases.IDLE
                or self.state.pending
                or self.state.pending_compaction is not None
                or self.state.interrupt_pending
            ):
                raise AgentBusy(
                    f"cannot rebind while phase={self.state.phase!r}",
                    phase=self.state.phase,
                )

            new_builder = (
                ctx_builder if ctx_builder is not None else self.ctx_builder
            )
            new_policy = (
                self.context_policy
                if context_policy is _UNSET else context_policy
            )
            if (
                new_policy is not None
                and getattr(new_policy, "enabled", False)
                and not hasattr(new_builder, "next_compaction")
            ):
                raise ValueError(
                    "context_policy requires a ContextBuilder with "
                    "next_compaction()"
                )

            if commit is not None:
                result = commit()
                if inspect.isawaitable(result):
                    await result

            self.ctx_builder = new_builder
            self.context_policy = new_policy
            if schemas is not _UNSET:
                self._schemas_provider = schemas
                if schemas is None:
                    self._static_schemas = ()
            if subagent_tools is not None:
                self._subagent_tools = frozenset(subagent_tools)
            if background_tools is not None:
                self._background_tools = frozenset(background_tools)

    def close(self) -> None:
        """Release the bus subscriptions (alias of :meth:`unsubscribe`)."""
        self.unsubscribe()

    @property
    def tools_schema(self) -> list:
        if self._schemas_provider is not None:
            return list(self._schemas_provider())
        return list(self._static_schemas)

    def _mine(self, ev: Event) -> bool:
        """This agent only reduces its own session's events (§11 partitioning)."""
        return ev.session_id == self.session_id

    # ══════════════ pure reducer steps ══════════════

    async def on_user(self, ev: Event) -> None:
        if not self._mine(ev):
            return
        self.state.observe(ev)
        message = {"role": "user", "content": ev.payload.get("text", "")}
        if self.state.phase == phases.IDLE:
            self.state.pending_compaction = None  # a new turn supersedes compaction
            self.state.messages.append(message)
            self.state.set_phase(phases.THINKING)
            self._tick()
            await self._intent("llm.request", ev)
            return
        # A step is already in flight: buffer the input and do not disturb the
        # provider stream.  It lands in ``messages`` at the step boundary (D7 /
        # §7.7).  A running compaction is deliberately not cancelled: it will
        # finish and ``_intent`` flushes this message.
        self.state.step_inbox.append(message)
        self._tick()

    async def on_llm(self, ev: Event) -> None:
        if not self._mine(ev):
            return
        interrupted = bool(ev.payload.get("interrupted"))
        # correlation: only the completion of the request we are waiting on is live
        if self.state.inflight_request_id is not None and ev.cause_id != self.state.inflight_request_id:
            return
        if not interrupted and self.state.interrupt_pending:
            interrupted = True  # fallback: no cancel wiring, tag the late completion anyway
        if not interrupted and self.state.phase != phases.THINKING:
            return  # late completion for a round that is no longer live (§16.3)
        self.state.observe(ev)

        message = dict(ev.payload["message"])
        if interrupted:
            # keep what the model produced before the interrupt, tag it, and do
            # NOT act on any partial tool call — the round is over (§16.3).
            message.pop("tool_calls", None)
            message["content"] = _tag_interrupt(message.get("content"))
            message["interrupted"] = True
            self.state.interrupt_pending = False
        self.state.messages.append(message)

        usage = ev.payload.get("usage") or {}
        cost = ev.payload.get("cost") or {}
        self.state.add_usage(
            input_tokens=usage.get("input_tokens", 0),
            output_tokens=usage.get("output_tokens", 0),
            usd=cost.get("total_usd", 0.0),
        )
        provider, actual = ev.payload.get("provider"), ev.payload.get("model_actual")
        if provider and actual:
            self.state.bound_model = f"{provider}/{actual}"  # record the pinned model (I25)

        if interrupted:                                     # round ends, tagged, no tools
            self._flush_inbox()                             # T5: never leave inbox behind
            if phases.can_transition(self.state.phase, phases.IDLE):
                self.state.set_phase(phases.IDLE)
            self._tick()
            return

        calls = ev.payload.get("tool_calls") or []
        if not calls:
            if self.state.step_inbox:
                # Inputs/notifications arrived during this step: consume them in
                # a new step instead of ending the turn (§7.9).
                self.state.round += 1
                self._tick()
                await self._intent("llm.request", ev)
                return
            self.state.set_phase(phases.IDLE)  # thinking -> idle
            self._tick()
            await self._publish(ev.child(
                "agent.completed", self._completed_payload(),
            ))
            return

        # fan out: register pending *before* publishing to avoid a race (§19).
        # Three disjoint routes: reserved background names, reserved subagent
        # names, and ordinary tools.  The model still speaks tool calls; only
        # the runtime fact differs (async ReAct design v0.1 §7.2).
        names = {call.get("call_id"): call.get("name") for call in calls}
        background_calls = [
            call for call in calls
            if names.get(call.get("call_id")) in self._background_tools
        ]
        subagent_calls = [
            call for call in calls
            if names.get(call.get("call_id")) in self._subagent_tools
        ]
        tool_calls = [
            call for call in calls
            if names.get(call.get("call_id")) not in self._background_tools
            and names.get(call.get("call_id")) not in self._subagent_tools
        ]
        self.state.set_phase(phases.ACTING)                 # thinking -> acting
        self.state.pending = {c["call_id"] for c in calls}
        self.state.pending_subagents = {c["call_id"] for c in subagent_calls}
        self.state.pending_background = {c["call_id"] for c in background_calls}
        self.state.set_phase(phases.AWAITING_TOOLS)         # acting -> awaiting_tools
        self._tick()
        for call in subagent_calls:
            await self._publish(ev.child("subagent.spawn.requested", {
                **dict(call.get("args") or {}),
                "call_id": call["call_id"],     # host-owned, never model-overridable
            }))
        for call in background_calls:
            await self._publish(ev.child(
                *self._background_intent(call),
            ))
        for call in tool_calls:
            await self._publish(ev.child("tool.call.requested", {
                "name": call["name"],
                "args": call.get("args") or {},
                "call_id": call["call_id"],
            }))

    async def on_tool_done(self, ev: Event) -> None:
        if not self._mine(ev):
            return
        if ev.payload.get("background"):
            # A background tool terminal is the manager's input, never a
            # foreground pairing (T4).  Observe only; no messages, no barrier.
            self.state.observe(ev)
            return
        call_id = ev.payload["call_id"]
        if call_id in self.state.cancelled:
            return  # result voided by an interrupt: never enters history (§16.3)
        self.state.observe(ev)
        failed = not ev.payload.get("ok", True)
        output = ev.payload.get("output")
        if output is None:
            output = ev.payload.get("error", "")
        content = output if isinstance(output, str) else json.dumps(output, ensure_ascii=False)
        if failed and ev.payload.get("safety"):        # safety policy blocked this call
            info = ev.payload.get("error_info") or {}
            kind = (info.get("context") or {}).get("kind", "policy")
            content = f"[blocked by safety policy: {kind}] {content}"
        message = {"role": "tool", "tool_call_id": call_id, "content": content}
        if ev.payload.get("output_ref") is not None:
            message["output_ref"] = ev.payload["output_ref"]
        if ev.payload.get("output_tokens") is not None:
            message["output_tokens"] = ev.payload["output_tokens"]
        self.state.messages.append(message)
        self.state.pending.discard(call_id)
        self.state.pending_subagents.discard(call_id)  # defensive replay cleanup
        self.state.pending_background.discard(call_id)
        await self._barrier(ev)

    async def on_subagent_notice(self, ev: Event) -> None:
        """Observe ``started`` / ``progress`` without touching messages or phase.

        These are UI/audit metadata.  Reducing them keeps ``last_ts`` and the
        replay anchor identical between a live run and ``replay_state`` (§6.1).
        """
        if not self._mine(ev):
            return
        self.state.observe(ev)

    async def on_subagent_done(self, ev: Event) -> None:
        """Pair one ``subagent.completed`` / ``subagent.failed`` (design §6.4).

        Usage is accounted **before** the cancellation/pending guards: a child
        that already spent tokens must hit the parent budget even when the
        result itself is voided by an interrupt.  ``subagent_usage_accounted``
        makes that at-most-once across duplicates, late events and replay.
        """
        if not self._mine(ev):
            return
        call_id = ev.payload.get("call_id")
        self.state.observe(ev)
        if ev.payload.get("background"):
            # Usage for a background child is accounted exactly once by
            # ``on_background_terminal`` (background_usage_accounted).  Doing it
            # here too would double-count the parent budget (§9.5 regression).
            return

        if call_id and call_id not in self.state.subagent_usage_accounted:
            usage = ev.payload.get("usage") or {}
            self.state.add_usage(
                input_tokens=usage.get("input_tokens", 0),
                output_tokens=usage.get("output_tokens", 0),
                usd=usage.get("usd", 0.0),
            )
            self.state.subagent_usage_accounted.add(call_id)

        if not call_id or call_id in self.state.cancelled:
            return  # voided by an interrupt: never enters history
        if call_id not in self.state.pending:
            return  # duplicate / late / post-restore terminal: never double-pair

        ok = bool(ev.payload.get("ok"))
        summary = ev.payload.get("summary") or ev.payload.get("error") or ""
        if not isinstance(summary, str):
            summary = json.dumps(summary, ensure_ascii=False)
        if not ok:
            code = ev.payload.get("code") or "E_SUBAGENT_FAILED"
            summary = f"[subagent failed: {code}] {summary}".rstrip()

        message = {
            "role": "tool",
            "tool_call_id": call_id,
            "content": summary,
            "subagent_session_id": ev.payload.get("child_session_id"),
        }
        if ev.payload.get("summary_ref") is not None:
            message["output_ref"] = ev.payload["summary_ref"]
        self.state.messages.append(message)
        self.state.pending.discard(call_id)
        self.state.pending_subagents.discard(call_id)
        self.state.pending_background.discard(call_id)

        # ★ the barrier covers tools and subagents alike (design §3.4)
        await self._barrier(ev)

    async def on_llm_failed(self, ev: Event) -> None:
        if not self._mine(ev):
            return
        if self.state.inflight_request_id is not None and ev.cause_id != self.state.inflight_request_id:
            return  # failure of a superseded request
        if self.state.phase != phases.THINKING:
            return  # late failure for a round that is no longer live (§16.3)
        self.state.observe(ev)
        self._flush_inbox()  # T5: an error path still returns to idle cleanly
        if phases.can_transition(self.state.phase, phases.IDLE):
            self.state.set_phase(phases.IDLE)
        self._tick()
        payload = {
            "round": self.state.round,
            "finish": "error",
            "code": ev.payload.get("code"),
            **self._background_summary(),
        }
        if ev.payload.get("error_info") is not None:   # carry structured detail through
            payload["error_info"] = ev.payload["error_info"]
        await self._publish(ev.child("agent.completed", payload))

    async def on_interrupt(self, ev: Event) -> None:
        if not self._mine(ev):
            return
        self.state.observe(ev)
        # cancelled round: stop in-flight tools and write a paired, interrupted
        # tool result for each one, so the assistant's tool_calls stay complete
        # and the model can see the round was cut short (§16.3).
        if self.state.pending:
            # Publish cancellation before writing the paired messages: the
            # orchestrator must never publish a terminal into the window where
            # the parent would pair it twice (design §6.5).
            if self.state.pending_subagents:
                await self._publish(ev.child("subagent.cancel.requested", {
                    "call_ids": sorted(self.state.pending_subagents),
                }))
            # ``pending_background`` also contains stop/list call ids; the
            # manager matches its own start handshakes and ignores the rest
            # (§7.8).  This closes a start that has not produced Started yet.
            if self.state.pending_background:
                await self._publish(ev.child("background.cancel.requested", {
                    "call_ids": sorted(self.state.pending_background),
                    "requested_by": "agent",
                }))
            foreground = (
                self.state.pending
                - self.state.pending_subagents
                - self.state.pending_background
            )
            if foreground:
                await self._publish(ev.child("tool.cancel.requested", {
                    "scope": "foreground",
                }))
            for call_id in sorted(self.state.pending):
                self.state.messages.append({
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": INTERRUPT_TAG,
                    "interrupted": True,
                })
            self.state.cancelled |= self.state.pending   # drop the late real results
        elif self.state.phase == phases.THINKING:
            # LLM in flight: remember to tag its (partial) completion when it arrives
            self.state.interrupt_pending = True

        # Background tasks are not killed by a user interrupt by default: they
        # are already asynchronous.  Opt-in via ``cancel_on_interrupt`` (§7.8).
        if self._cancel_on_interrupt and self.state.background:
            running = self.state.background_running_ids()
            if running:
                await self._publish(ev.child("background.cancel.requested", {
                    "task_ids": running,
                    "reason": "user interrupt",
                    "requested_by": "agent",
                }))

        self.state.pending.clear()
        self.state.pending_subagents.clear()
        self.state.pending_background.clear()
        self.state.pending_compaction = None
        self._flush_inbox()  # T5: idle implies an empty step inbox
        if phases.can_transition(self.state.phase, phases.IDLE):
            self.state.set_phase(phases.IDLE)
        self._tick()

    async def on_compaction_applied(self, ev: Event) -> None:
        """Apply a recorded summary and re-issue the request it was blocking."""
        if not self._mine(ev):
            return
        payload = ev.payload
        pending = self.state.pending_compaction
        start = int(payload.get("from_index", -1))
        end = int(payload.get("to_index", -1))
        valid_range = 0 <= start < end <= len(self.state.messages)
        if pending is None:
            if self._replaying and valid_range and self._is_next_compaction(start):
                self.state.observe(ev)
                self.state.compactions.append(self._compaction_record(payload))
            return  # stale / already superseded in the live loop
        if (
            payload.get("source_digest") != pending.get("source_digest")
            or start != pending.get("from_index")
            or end != pending.get("to_index")
            or not valid_range
        ):
            self.state.pending_compaction = None
            plan = await self.ctx_builder.build(
                self.state, ev=ev, tools=self.tools_schema
            )
            await self._context_overflow(
                ev, plan,
                message="compaction result was stale or invalid",
            )
            return
        self.state.observe(ev)
        self.state.compactions.append(self._compaction_record(payload))
        self.state.pending_compaction = None
        self._tick()
        await self._intent("llm.request", ev)

    def _is_next_compaction(self, start: int) -> bool:
        last_to = max(
            (int(record["to_index"]) for record in self.state.compactions), default=0
        )
        return start == last_to

    @staticmethod
    def _compaction_record(payload: dict) -> dict:
        return {
            "from_index": int(payload.get("from_index", 0)),
            "to_index": int(payload.get("to_index", 0)),
            "summary": payload.get("summary", ""),
            "summary_ref": payload.get("summary_ref"),
            "source_digest": payload.get("source_digest", ""),
            "tokens": int(payload.get("tokens", 0)),
            "fallback": bool(payload.get("fallback", False)),
        }

    async def on_approval(self, ev: Event) -> None:
        if not self._mine(ev):
            return
        self.state.observe(ev)
        if self.state.phase == phases.AWAITING_APPROVAL:
            self.state.set_phase(phases.THINKING)
            self._tick()
            await self._intent("llm.request", ev)

    async def on_resumed(self, ev: Event) -> None:
        if not self._mine(ev):
            return
        self.state.observe(ev)
        if self.state.phase == phases.PAUSED:
            self.state.set_phase(phases.THINKING)
            self._tick()
            await self._intent("llm.request", ev)

    async def on_tools_changed(self, ev: Event) -> None:
        """Plugins loaded/unloaded -> rebuild the schema so the LLM sees them (§27.1)."""
        del ev
        _ = self.tools_schema

    # ══════════════ background reducer steps (§7) ══════════════

    @staticmethod
    def _background_intent(call: dict) -> tuple:
        """Map one reserved background call to its intent event (§7.2).

        Malformed argument shapes are **not** raised here — the manager returns
        ``background.start.failed`` / ``background.stop.failed`` so a bad model
        JSON cannot crash the reducer (errors are data, P4).
        """
        name = call.get("name")
        raw = call.get("args")
        args = raw if isinstance(raw, dict) else {}
        call_id = call.get("call_id")
        if name == "start":
            payload = {
                "call_id": call_id,
                "tool": args.get("tool") if isinstance(raw, dict) else None,
                "args": args.get("args") if isinstance(raw, dict) else None,
            }
            if "reason" in args:
                payload["reason"] = args.get("reason")
            return "background.start.requested", payload
        if name == "stop":
            payload = {
                "call_id": call_id,
                "task_id": args.get("task_id") if isinstance(raw, dict) else None,
            }
            if "reason" in args:
                payload["reason"] = args.get("reason")
            return "background.stop.requested", payload
        return "background.list.requested", {"call_id": call_id}

    async def on_background_started(self, ev: Event) -> None:
        """Pair one ``background.started`` with its ``start`` tool call (§7.3)."""
        if not self._mine(ev):
            return
        self.state.observe(ev)
        payload = ev.payload if isinstance(ev.payload, dict) else {}
        call_id = payload.get("call_id")
        if not call_id or call_id in self.state.cancelled:
            return  # voided by an interrupt: never enters history
        if (
            call_id not in self.state.pending
            or call_id not in self.state.pending_background
        ):
            return  # duplicate / late
        task_id = payload.get("task_id")
        content = json.dumps({
            "status": "started",
            "task_id": task_id,
            "tool": payload.get("name"),
            "kind": payload.get("kind", "tool"),
            "note": "Use stop(task_id=...) to cancel.",
        }, ensure_ascii=False)
        self.state.messages.append({
            "role": "tool",
            "tool_call_id": call_id,
            "content": content,
        })
        self.state.pending.discard(call_id)
        self.state.pending_background.discard(call_id)
        if isinstance(task_id, str) and task_id:
            self.state.background[task_id] = {
                "task_id": task_id,
                "name": payload.get("name"),
                "kind": payload.get("kind", "tool"),
                "status": "running",
                "start_call_id": call_id,
                "started_seq": int(ev.seq or 0),
                "finished_seq": None,
                "output_ref": None,
                "code": None,
                "error": None,
            }
        await self._barrier(ev)

    async def on_background_start_failed(self, ev: Event) -> None:
        """Pair a rejected ``start`` and advance the barrier (§7.3)."""
        if not self._mine(ev):
            return
        self.state.observe(ev)
        payload = ev.payload if isinstance(ev.payload, dict) else {}
        call_id = payload.get("call_id")
        if not call_id or call_id in self.state.cancelled:
            return
        if (
            call_id not in self.state.pending
            or call_id not in self.state.pending_background
        ):
            return
        content = json.dumps({
            "status": "failed",
            "code": payload.get("code"),
            "error": payload.get("error"),
        }, ensure_ascii=False)
        self.state.messages.append({
            "role": "tool",
            "tool_call_id": call_id,
            "content": self._clip_content(content),
        })
        self.state.pending.discard(call_id)
        self.state.pending_background.discard(call_id)
        await self._barrier(ev)

    async def on_background_stop_done(self, ev: Event) -> None:
        """Pair ``background.stop.completed`` / ``.failed`` (§7.4)."""
        if not self._mine(ev):
            return
        self.state.observe(ev)
        payload = ev.payload if isinstance(ev.payload, dict) else {}
        call_id = payload.get("call_id")
        if not call_id or call_id in self.state.cancelled:
            return
        if (
            call_id not in self.state.pending
            or call_id not in self.state.pending_background
        ):
            return
        if ev.type == "background.stop.failed":
            content = json.dumps({
                "status": "failed",
                "code": payload.get("code"),
                "error": payload.get("error"),
            }, ensure_ascii=False)
        else:
            content = json.dumps({
                "status": payload.get("status", "stopping"),
                "task_id": payload.get("task_id"),
                "already_finished": bool(payload.get("already_finished")),
            }, ensure_ascii=False)
        self.state.messages.append({
            "role": "tool",
            "tool_call_id": call_id,
            "content": self._clip_content(content),
        })
        self.state.pending.discard(call_id)
        self.state.pending_background.discard(call_id)
        await self._barrier(ev)

    async def on_background_list_done(self, ev: Event) -> None:
        """Pair ``background.list.completed`` (§7.4)."""
        if not self._mine(ev):
            return
        self.state.observe(ev)
        payload = ev.payload if isinstance(ev.payload, dict) else {}
        call_id = payload.get("call_id")
        if not call_id or call_id in self.state.cancelled:
            return
        if (
            call_id not in self.state.pending
            or call_id not in self.state.pending_background
        ):
            return
        content = json.dumps(
            {"tasks": payload.get("tasks") or []}, ensure_ascii=False,
        )
        self.state.messages.append({
            "role": "tool",
            "tool_call_id": call_id,
            "content": self._clip_content(content),
        })
        self.state.pending.discard(call_id)
        self.state.pending_background.discard(call_id)
        await self._barrier(ev)

    async def on_background_terminal(self, ev: Event) -> None:
        """Reduce one task terminal into a normal user-role notification (§7.5).

        Usage is accounted **before** the metadata guard so a child that already
        spent tokens still hits the parent budget when its ``Started`` result was
        voided by an interrupt.  ``background_usage_accounted`` keeps that
        at-most-once across duplicates and replay.
        """
        if not self._mine(ev):
            return
        payload = ev.payload if isinstance(ev.payload, dict) else {}
        task_id = payload.get("task_id")
        self.state.observe(ev)

        usage = payload.get("usage") or {}
        if (
            isinstance(task_id, str)
            and task_id
            and usage
            and task_id not in self.state.background_usage_accounted
        ):
            self.state.add_usage(
                input_tokens=usage.get("input_tokens", 0),
                output_tokens=usage.get("output_tokens", 0),
                usd=usage.get("usd", 0.0),
            )
            self.state.background_usage_accounted.add(task_id)

        meta = self.state.background.get(task_id)
        if meta is None:
            return  # unknown / pruned / start was voided: account only, no notice
        status = payload.get("status") or ev.type.rsplit(".", 1)[-1]
        if meta.get("status") in (
            "completed", "failed", "stopped", "lost",
        ):
            return  # T2: first terminal wins

        meta.update({
            "status": status,
            "finished_seq": int(ev.seq or 0),
            "output_ref": (
                payload.get("output_ref") or payload.get("summary_ref")
            ),
            "code": payload.get("code"),
            "error": payload.get("error"),
            "duration_ms": payload.get("duration_ms"),
            "recovered": bool(payload.get("recovered", False)),
        })
        self.state.prune_background(keep_task_id=task_id)

        message = self._background_notification(ev, payload, status)
        if self.state.phase == phases.IDLE:
            self.state.messages.append(message)
        else:
            self.state.step_inbox.append(message)  # D7: flush at step end

        auto_continue = (
            not self._replaying
            and self.state.phase == phases.IDLE
            and not payload.get("recovered")
            and ev.type in ("background.completed", "background.failed")
            and self._auto_continue
        )
        if auto_continue:
            self.state.set_phase(phases.THINKING)
            self.state.round += 1
        self._tick()
        if auto_continue:
            await self._intent("llm.request", ev)

    def _background_notification(self, ev: Event, payload: dict, status: str) -> dict:
        task_id = payload.get("task_id")
        name = payload.get("name") or "task"
        kind = payload.get("kind") or "tool"
        lines = [f"[background task {task_id}] status: {status}"]
        lines.append(f"{'subagent' if kind == 'subagent' else 'tool'}: {name}")
        if payload.get("duration_ms") is not None:
            lines.append(f"duration_ms: {int(payload['duration_ms'])}")
        body = payload.get("output")
        if kind == "subagent" and payload.get("summary") is not None:
            body = payload.get("summary")
        if body is not None:
            if not isinstance(body, str):
                body = json.dumps(body, ensure_ascii=False)
            if body:
                lines.append("output:")
                lines.append(body)
        if status == "failed":
            if payload.get("code"):
                lines.append(f"code: {payload['code']}")
            if payload.get("error"):
                lines.append(f"error: {payload['error']}")
        elif status == "stopped":
            lines.append(
                f"requested_by: {payload.get('requested_by') or 'unknown'}"
            )
            if payload.get("reason"):
                lines.append(f"reason: {payload['reason']}")
        elif status == "lost":
            lines.append(f"reason: {payload.get('reason') or 'process restart'}")
        content = self._clip_content("\n".join(lines))
        message = {
            "role": "user",
            "content": content,
            "background_notification": True,
            "background_task_id": task_id,
            "background_status": status,
        }
        if payload.get("output_ref") is not None:
            message["output_ref"] = payload["output_ref"]
        if payload.get("summary_ref") is not None:
            message["output_ref"] = payload["summary_ref"]
        return message

    @staticmethod
    def _clip_content(content: str) -> str:
        """Keep one message under ``INLINE_LIMIT`` (I15 / §10.1)."""
        encoded = content.encode("utf-8")
        budget = INLINE_LIMIT - 512
        if len(encoded) <= budget:
            return content
        text = encoded[:budget].decode("utf-8", "ignore")
        return text + "\n…(truncated)"

    async def _barrier(self, ev: Event) -> None:
        """The convergence barrier for tools, subagents and background calls."""
        if self.state.pending:
            self.state.set_phase(phases.AWAITING_TOOLS)
            self._tick()
            return
        self.state.set_phase(phases.THINKING)
        self.state.round += 1
        self._tick()
        await self._intent("llm.request", ev)

    # -- step inbox / completed payload helpers -----------------------------
    def _flush_inbox(self) -> None:
        """Move buffered inputs into history immediately before a request."""
        if self.state.step_inbox:
            self.state.messages.extend(self.state.step_inbox)
            self.state.step_inbox.clear()

    def _background_summary(self) -> dict:
        ids = self.state.background_running_ids()
        return {"background_running": len(ids), "background_task_ids": ids}

    def _completed_payload(self) -> dict:
        return {"round": self.state.round, **self._background_summary()}

    # ══════════════ intents & bookkeeping ══════════════

    async def _intent(self, etype: str, parent: Event) -> None:
        if self._replaying:
            return
        if (reason := self.state.budget.check(self.state)):
            if phases.can_transition(self.state.phase, phases.PAUSED):
                self.state.set_phase(phases.PAUSED)
            self._tick()
            info = error_info("E_BUDGET", source=ErrorSource.AGENT.value,
                              location="linteum_agent.runtime.agent.Agent._intent",
                              message=reason, enters_history=False).to_dict()
            await self._publish(parent.child("system.budget.exceeded",
                                                {"reason": reason, "code": "E_BUDGET",
                                                 "round": self.state.round,
                                                 "error_info": info}))
            return
        had_inbox = bool(self.state.step_inbox)
        self._flush_inbox()
        if had_inbox:
            self._tick()  # the flush is a state change; keep version/seq aligned
        tools = self.tools_schema
        plan = await self.ctx_builder.build(self.state, ev=parent, tools=tools)

        policy = self.context_policy
        if policy is not None and policy.enabled and plan.estimated_tokens() > policy.max_tokens:
            if self.state.pending_compaction is not None:
                return
            choose = getattr(self.ctx_builder, "next_compaction", None)
            compacted = choose(self.state, plan, policy) if choose is not None else None
            if not compacted:
                await self._context_overflow(parent, plan)
                return
            messages = [
                normalise_message(message)
                for message in self.state.messages[compacted.from_index:compacted.to_index]
            ]
            request = parent.child("context.compaction.requested", {
                "from_index": compacted.from_index,
                "to_index": compacted.to_index,
                "source_digest": plan.digest,
                "target_tokens": policy.max_summary_tokens,
                "messages": messages,
            })
            self.state.pending_compaction = {
                "from_index": compacted.from_index,
                "to_index": compacted.to_index,
                "source_digest": plan.digest,
            }
            self._tick()
            try:
                await self._publish(request)
            except BusError as exc:
                self.state.pending_compaction = None
                await self._context_overflow(parent, plan, code=exc.code,
                                             message=str(exc))
            return

        request = parent.child(etype, {
            "context_digest": plan.digest,
            "messages": plan.to_provider_messages(),
            "tools": list(tools),
            "round": self.state.round,
            "model": self.model,
        })
        # a new request supersedes any prior one: correlate completions by cause id
        self.state.inflight_request_id = request.id
        self.state.last_context_digest = plan.digest
        self.state.interrupt_pending = False
        await self._publish(request)

    async def _context_overflow(self, parent: Event, plan, *,
                                code: str = "E_CONTEXT_OVERFLOW",
                                message: str = "context exceeds budget with no compactable turn") -> None:
        self._flush_inbox()  # T5: this path returns the session to idle
        if phases.can_transition(self.state.phase, phases.IDLE):
            self.state.set_phase(phases.IDLE)
        self._tick()
        info = error_info(
            code,
            source=ErrorSource.AGENT.value,
            location="linteum_agent.runtime.agent.Agent._intent",
            message=message,
            enters_history=False,
            context={"context_tokens": plan.estimated_tokens()},
        ).to_dict()
        await self._publish(parent.child("agent.completed", {
            "round": self.state.round,
            "finish": "error",
            "code": code,
            "error_info": info,
            "context_tokens": plan.estimated_tokens(),
            **self._background_summary(),
        }))

    async def _publish(self, ev: Event) -> None:
        if self._replaying or self.bus is None:
            return
        await self.bus.publish(ev)

    def _tick(self) -> None:
        if self._replaying:
            return
        self.state.version += 1
        self.state.seq += 1
        if self.checkpointer is not None:
            self.checkpointer.maybe_snapshot(self.state)

    # -- recovery -----------------------------------------------------------
    @classmethod
    async def restore(cls, bus, checkpointer: Checkpointer, *, session_id: str,
                      pending_approvals: Sequence[str] = (), **kwargs) -> "Agent":
        """Rebuild an agent from the latest checkpoint + its WAL tail (P1).

        ``pending_approvals`` lists ``call_id`` values whose approval is still
        open in the (restored) :class:`ApprovalGate`.  The half-round closer must
        keep those calls pending instead of declaring the round interrupted,
        otherwise a server restart silently drops a human-in-the-loop tool call.
        """
        kwargs.pop("auto_subscribe", None)
        kwargs.pop("replay", None)
        agent = cls(bus, session_id=session_id, checkpointer=checkpointer,
                    auto_subscribe=False, **kwargs)
        checkpoint = checkpointer.latest(session_id)
        events = await bus.replay(
            session_id=session_id, after_seq=checkpoint.seq if checkpoint else 0
        )
        artifacts = getattr(bus, "artifacts", None)
        if artifacts is None:
            artifacts = NullArtifactStore()
        events = await hydrate_events(events, artifacts)
        if checkpoint is not None:
            state = AgentState.from_dict(checkpoint.state)
            state.last_event_seq = max(state.last_event_seq, checkpoint.seq)
        else:
            trace_id = events[0].trace_id if events else uuid7()
            state = AgentState(session_id=session_id, trace_id=trace_id)
        agent.state = await cls.replay_state(state, events)
        await agent._close_half_round(pending_approvals=pending_approvals)
        agent.subscribe()
        return agent

    @classmethod
    async def replay_state(cls, state: AgentState, events: Sequence[Event]) -> AgentState:
        """Apply committed events to ``state`` without publishing or snapshotting."""
        replay = cls(
            None,
            session_id=state.session_id,
            auto_subscribe=False,
            replay=True,
        )
        replay.state = state
        for ev in events:
            if ev.session_id != state.session_id:
                continue
            await replay._reduce_replay(ev)
        return replay.state

    async def _reduce_replay(self, ev: Event) -> None:
        etype = ev.type
        if etype == "user.message":
            await self.on_user(ev)
        elif etype == "user.interrupt":
            await self.on_interrupt(ev)
        elif etype == "llm.completed":
            await self.on_llm(ev)
        elif etype == "llm.failed":
            await self.on_llm_failed(ev)
        elif etype in ("tool.call.completed", "tool.call.failed"):
            await self.on_tool_done(ev)
        elif etype in ("subagent.completed", "subagent.failed"):
            await self.on_subagent_done(ev)
        elif etype in ("subagent.started", "subagent.progress"):
            self.state.observe(ev)
        elif etype == "subagent.spawn.requested":
            self.state.observe(ev)
            call_id = ev.payload.get("call_id")
            if call_id:
                # Trust the WAL fact, not the *current* host configuration: a
                # session recorded with subagents enabled must replay the same
                # routing even when the feature is now disabled (design §6.6).
                self.state.pending.add(call_id)
                self.state.pending_subagents.add(call_id)
        # -- background: WAL-derived routing, never config-derived (§7.14) --
        elif etype in (
            "background.start.requested",
            "background.stop.requested",
            "background.list.requested",
        ):
            self.state.observe(ev)
            call_id = ev.payload.get("call_id")
            if call_id:
                self.state.pending.add(call_id)
                self.state.pending_background.add(call_id)
        elif etype == "background.started":
            await self.on_background_started(ev)
        elif etype == "background.start.failed":
            await self.on_background_start_failed(ev)
        elif etype in (
            "background.stop.completed", "background.stop.failed",
        ):
            await self.on_background_stop_done(ev)
        elif etype == "background.list.completed":
            await self.on_background_list_done(ev)
        elif etype in (
            "background.completed", "background.failed",
            "background.stopped", "background.lost",
        ):
            await self.on_background_terminal(ev)
        elif etype == "background.cancel.requested":
            self.state.observe(ev)
        elif etype == "approval.granted":
            await self.on_approval(ev)
        elif etype == "agent.resumed":
            await self.on_resumed(ev)
        elif etype == "context.compaction.requested":
            # Live flushes before building the compaction range; replay must do
            # the same or ``applied.valid_range`` diverges (§7.6).
            self._flush_inbox()
            self.state.observe(ev)
        elif etype == "context.compaction.applied":
            self._flush_inbox()
            await self.on_compaction_applied(ev)
        elif etype == "agent.completed":
            self._flush_inbox()
            self.state.observe(ev)
            self.state.round = max(
                self.state.round,
                int((ev.payload or {}).get("round") or 0),
            )
            if self.state.phase != phases.IDLE and phases.can_transition(
                self.state.phase, phases.IDLE
            ):
                self.state.set_phase(phases.IDLE)
        elif etype == "system.budget.exceeded":
            self.state.observe(ev)
            if self.state.phase == phases.IDLE:
                # Mirror live auto-continue: the request entered thinking before
                # the budget check paused it (IDLE→PAUSED is not legal).
                self.state.set_phase(phases.THINKING)
            if self.state.phase == phases.THINKING:
                self.state.set_phase(phases.PAUSED)
            self.state.round = max(
                self.state.round,
                int((ev.payload or {}).get("round") or self.state.round),
            )
        elif etype == "llm.request":
            # Live flushes immediately before this publish; replay must land the
            # buffered inputs before the request is observed (§7.6).
            self._flush_inbox()
            self.state.observe(ev)
            self.state.inflight_request_id = ev.id
            self.state.last_context_digest = ev.payload.get("context_digest", "")
            self.state.round = max(
                self.state.round, int(ev.payload.get("round") or 0)
            )
            if self.state.phase == phases.IDLE:
                # A recorded request proves the live session was thinking, even
                # when today's ``auto_continue`` differs (WAL-derived replay).
                self.state.set_phase(phases.THINKING)

    async def _close_half_round(self, *, pending_approvals: Sequence[str] = ()) -> None:
        """Close a round interrupted by a process restart (no provider call).

        Approval-gated calls are special: their ``tool.call.requested`` fact is
        still valid and the human decision will arrive after the restart, so the
        round must stay alive.  Non-approval siblings (which were genuinely
        interrupted) are paired and voided as usual.
        """
        if self.state.phase == phases.PAUSED:
            return
        if self.state.phase not in (
            phases.THINKING,
            phases.ACTING,
            phases.AWAITING_TOOLS,
            phases.AWAITING_APPROVAL,
        ):
            return
        # Buffered inputs are already-recorded facts: land them before the
        # half-round is closed so they are not lost (T5 / §7.13).
        self._flush_inbox()
        # A recovered process has no live orchestrator; tell any still-running
        # one to close the child before pairing the round as interrupted.
        if self.state.pending_subagents:
            await self._publish(Event.create(
                "subagent.cancel.requested",
                {"call_ids": sorted(self.state.pending_subagents)},
                session_id=self.session_id,
                trace_id=self.state.trace_id,
            ))
        approvals = set(pending_approvals) & self.state.pending
        # A start handshake that is not waiting for approval cannot survive a
        # restart: tell the manager to close it (it ignores stop/list ids).
        cancel_starts = self.state.pending_background - approvals
        if cancel_starts:
            await self._publish(Event.create(
                "background.cancel.requested",
                {
                    "call_ids": sorted(cancel_starts),
                    "reason": "process restart",
                    "requested_by": "agent",
                },
                session_id=self.session_id,
                trace_id=self.state.trace_id,
            ))
        if approvals:
            for call_id in sorted(self.state.pending - approvals):
                self.state.messages.append({
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": INTERRUPT_TAG,
                    "interrupted": True,
                })
                self.state.cancelled.add(call_id)
            self.state.pending = set(approvals)
            # Approvals are tool calls, never subagent calls.
            self.state.pending_subagents &= approvals
            self.state.pending_background &= approvals
            self.state.pending_compaction = None
            self.state.interrupt_pending = False
            self._tick()
            return  # keep the approval round open for a post-restart decision
        round_number = self.state.round
        if self.state.pending:
            for call_id in sorted(self.state.pending):
                self.state.messages.append({
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": INTERRUPT_TAG,
                    "interrupted": True,
                })
            self.state.cancelled |= self.state.pending
        self.state.pending.clear()
        self.state.pending_subagents.clear()
        self.state.pending_background.clear()
        self.state.pending_compaction = None
        self.state.interrupt_pending = False
        if phases.can_transition(self.state.phase, phases.IDLE):
            self.state.set_phase(phases.IDLE)
        self._tick()
        await self._publish(
            Event.create(
                "agent.completed",
                {
                    "round": round_number,
                    "finish": "interrupted",
                    "recovered": True,
                    "reason": "process restart",
                    **self._background_summary(),
                },
                session_id=self.session_id,
                trace_id=self.state.trace_id,
            )
        )

