"""Event-driven agent runtime (migrated from the ``new_projer`` framework).

Layers, bottom-up:

* :mod:`linteum_agent.bus` — the event bus (top-level package: Event, Router,
  WFQ, WAL, artifact, backpressure, namespace, metrics).
* :mod:`linteum_agent.runtime.state` — :class:`AgentState`, the phase machine
  and checkpointing.
* :mod:`linteum_agent.runtime.context` — :class:`ContextPlan` + deterministic digest.
* :mod:`linteum_agent.runtime.compaction` — deterministic summary effect for context compaction.
* :mod:`linteum_agent.runtime.agent` — the pure, event-driven :class:`Agent` reducer.
* :mod:`linteum_agent.runtime.host` — :class:`AgentHost`: storage/bus/LLM/tools
  assembly, one agent per session, restore and pending-approval recovery.
* :mod:`linteum_agent.runtime.modes` — session-scoped mode definitions, catalog,
  selector resolution and prompt/environment rendering.
* :mod:`linteum_agent.runtime.interrupt` — trips the LLM cancel token on ``user.interrupt``.
* :mod:`linteum_agent.runtime.tools` — ToolSpec / ToolRegistry / ToolRuntime / Sandbox.
* :mod:`linteum_agent.runtime.llm` — canonical types, provider adapters (OpenAI /
  DeepSeek / Anthropic), streaming accumulator, routing / registry / breaker /
  cache, pricing and the :class:`LLMExecutor` (the only ``llm.request`` subscriber).
* :mod:`linteum_agent.runtime.errors` — structured ``error_info`` (re-exported from the bus).
* :mod:`linteum_agent.runtime.observability` — dependency-free structured run-log
  sidecar (subscribes ``bus.on_any`` + bus internal log hooks).

Importing this package has no side effects so hosts can load submodules lazily.
"""

__all__ = [
    "agent",
    "host",
    "state",
    "context",
    "compaction",
    "interrupt",
    "modes",
    "tools",
    "llm",
    "errors",
    "observability",
]
