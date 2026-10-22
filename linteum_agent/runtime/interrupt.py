"""InterruptController: trip the LLM cancel registry on ``user.interrupt`` (§16.3).

The :class:`~linteum_agent.runtime.agent.Agent` reducer stays pure: on ``user.interrupt`` it clears
the ``pending`` barrier and emits a ``tool.cancel.requested`` intent so the tool
layer stops in-flight calls.  It cannot, however, reach the LLM executor's
*cooperative cancel token* — that is an effect-layer concern (the reducer owns no
I/O).  This component closes that gap.

It subscribes to ``user.interrupt`` and trips the session's token in the shared
:class:`~linteum_agent.bus.scheduler.cancel.CancelRegistry`.  The executor checks that token
once per streamed chunk (``token.raise_if_cancelled()``), so the provider stream
aborts promptly and surfaces as ``llm.failed{E_CANCELLED}`` — which the Agent then
drops because the round is no longer live.

Wiring::

    registry = CancelRegistry()
    executor = LLMExecutor(bus, router, cancel=registry)
    agent = Agent(bus, session_id="s1")
    InterruptController(bus, registry)          # same registry, bound to the bus
"""

from __future__ import annotations

from ..bus.scheduler.cancel import CancelRegistry


class InterruptController:
    """Bind ``user.interrupt`` to a session's LLM cancel token.

    Kept deliberately small: it is the effect-layer counterpart to
    :meth:`Agent.on_interrupt <linteum_agent.runtime.agent.Agent.on_interrupt>`, not a second
    reducer.  Tool cancellation is emitted by the Agent itself; this only reaches
    the piece the Agent cannot (the executor's token).
    """

    def __init__(
        self,
        bus,
        registry: CancelRegistry,
        *,
        auto_subscribe: bool = True,
    ) -> None:
        self.bus = bus
        self.registry = registry
        if auto_subscribe:
            self.subscribe()

    def subscribe(self) -> None:
        self.bus.on("user.interrupt", self.on_interrupt)

    async def on_interrupt(self, ev) -> None:
        self.registry.cancel(ev.session_id)
