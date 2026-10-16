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

import json
from typing import Optional

from ..bus.event import Event, uuid7
from .context import ContextBuilder, PassthroughContextBuilder
from .errors import ErrorSource, error_info
from .state import AgentState, Budget, phases
from .state.checkpoint import Checkpointer

#: Marker appended to a message whose round the user interrupted (§16.3).  It is
#: written into ``content`` (so it survives into the provider context) and paired
#: with an ``interrupted`` flag host UIs can read.
INTERRUPT_TAG = "[interrupted by user]"


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
        auto_subscribe: bool = True,
    ) -> None:
        self.bus = bus
        self.session_id = session_id
        self.model = model
        self.state = AgentState(session_id=session_id, trace_id=uuid7(), budget=budget or Budget())
        self.checkpointer = checkpointer
        self.ctx_builder = ctx_builder or PassthroughContextBuilder()
        self._schemas_provider = schemas          # callable -> list[dict] (ToolRegistry.schemas)
        self._static_schemas = tuple(tools_schema)
        if auto_subscribe:
            self.subscribe()

    # -- subscription -------------------------------------------------------
    def subscribe(self) -> None:
        bus = self.bus
        bus.on("user.message", self.on_user)
        bus.on("user.interrupt", self.on_interrupt)
        bus.on("llm.completed", self.on_llm)
        bus.on("llm.failed", self.on_llm_failed)
        bus.on("tool.call.completed", self.on_tool_done)
        bus.on("tool.call.failed", self.on_tool_done)
        bus.on("approval.granted", self.on_approval)
        bus.on("agent.resumed", self.on_resumed)
        bus.on("system.plugin.loaded", self.on_tools_changed)
        bus.on("system.plugin.unloaded", self.on_tools_changed)

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
        self.state.messages.append({"role": "user", "content": ev.payload.get("text", "")})
        self.state.set_phase(phases.THINKING)
        self._tick()
        await self._intent("llm.request", ev)

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
            if phases.can_transition(self.state.phase, phases.IDLE):
                self.state.set_phase(phases.IDLE)
            self._tick()
            return

        calls = ev.payload.get("tool_calls") or []
        if not calls:
            self.state.set_phase(phases.IDLE)  # thinking -> idle
            self._tick()
            await self.bus.publish(ev.child("agent.completed", {"round": self.state.round}))
            return

        # fan out: register pending *before* publishing to avoid a race (§19)
        self.state.set_phase(phases.ACTING)                 # thinking -> acting
        self.state.pending = {c["call_id"] for c in calls}
        self.state.set_phase(phases.AWAITING_TOOLS)         # acting -> awaiting_tools
        self._tick()
        for call in calls:
            await self.bus.publish(ev.child("tool.call.requested", {
                "name": call["name"],
                "args": call.get("args") or {},
                "call_id": call["call_id"],
            }))

    async def on_tool_done(self, ev: Event) -> None:
        if not self._mine(ev):
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
        self.state.messages.append(
            {"role": "tool", "tool_call_id": call_id, "content": content}
        )
        self.state.pending.discard(call_id)

        if self.state.pending:  # still waiting on siblings -> remain in the barrier
            self.state.set_phase(phases.AWAITING_TOOLS)
            self._tick()
            return
        # ★ convergence barrier: every branch done -> think again (§18.2, I2)
        self.state.set_phase(phases.THINKING)
        self.state.round += 1
        self._tick()
        await self._intent("llm.request", ev)

    async def on_llm_failed(self, ev: Event) -> None:
        if not self._mine(ev):
            return
        if self.state.inflight_request_id is not None and ev.cause_id != self.state.inflight_request_id:
            return  # failure of a superseded request
        if self.state.phase != phases.THINKING:
            return  # late failure for a round that is no longer live (§16.3)
        self.state.observe(ev)
        if phases.can_transition(self.state.phase, phases.IDLE):
            self.state.set_phase(phases.IDLE)
        self._tick()
        payload = {
            "round": self.state.round,
            "finish": "error",
            "code": ev.payload.get("code"),
        }
        if ev.payload.get("error_info") is not None:   # carry structured detail through
            payload["error_info"] = ev.payload["error_info"]
        await self.bus.publish(ev.child("agent.completed", payload))

    async def on_interrupt(self, ev: Event) -> None:
        if not self._mine(ev):
            return
        self.state.observe(ev)
        # cancelled round: stop in-flight tools and write a paired, interrupted
        # tool result for each one, so the assistant's tool_calls stay complete
        # and the model can see the round was cut short (§16.3).
        if self.state.pending:
            await self.bus.publish(ev.child("tool.cancel.requested", {}))
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
        self.state.pending.clear()
        if phases.can_transition(self.state.phase, phases.IDLE):
            self.state.set_phase(phases.IDLE)
        self._tick()

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

    # ══════════════ intents & bookkeeping ══════════════

    async def _intent(self, etype: str, parent: Event) -> None:
        if (reason := self.state.budget.check(self.state)):
            if phases.can_transition(self.state.phase, phases.PAUSED):
                self.state.set_phase(phases.PAUSED)
            self._tick()
            info = error_info("E_BUDGET", source=ErrorSource.AGENT.value,
                              location="linteum_agent.runtime.agent.Agent._intent",
                              message=reason, enters_history=False).to_dict()
            await self.bus.publish(parent.child("system.budget.exceeded",
                                                {"reason": reason, "code": "E_BUDGET",
                                                 "error_info": info}))
            return
        plan = await self.ctx_builder.build(self.state, ev=parent, tools=self.tools_schema)
        request = parent.child(etype, {
            "context_digest": plan.digest,
            "messages": plan.to_provider_messages(),
            "tools": list(self.tools_schema),
            "round": self.state.round,
            "model": self.model,
        })
        # a new request supersedes any prior one: correlate completions by cause id
        self.state.inflight_request_id = request.id
        self.state.interrupt_pending = False
        await self.bus.publish(request)

    def _tick(self) -> None:
        self.state.version += 1
        self.state.seq += 1
        if self.checkpointer is not None:
            self.checkpointer.maybe_snapshot(self.state)

    # -- recovery -----------------------------------------------------------
    @classmethod
    def restore(cls, bus, checkpointer: Checkpointer, *, session_id: str, **kwargs) -> "Agent":
        """Rebuild an agent from the latest checkpoint, then keep reducing (P1)."""
        agent = cls(bus, session_id=session_id, checkpointer=checkpointer, **kwargs)
        restored = checkpointer.restore(session_id)
        if restored is not None:
            linteum_agent.runtime.state = restored
        return agent

