"""The linteum agent library: the runtime behind the ``linteum`` command-line tool.

Module layout:

- ``bus``: event-driven runtime bus (immutable ``Event`` envelope, RCU router,
  per-origin fair scheduling, WAL durability, backpressure/coalescing, namespace
  isolation) migrated from the experimental ``new_projer`` framework;
- ``config``: user-level config (``~/.agent/config.yaml``) read/write;
- ``context``: conversation / session context storage;
- ``messages``: message data models;
- ``model``: LLM clients (OpenAI / Anthropic / compatible endpoints);
- ``prompt``: system prompt template loading;
- ``runtime``: the event-driven agent runtime (pure ``Agent`` reducer, state /
  phase machine / checkpoints, context plan, tool runtime + sandbox, canonical
  LLM types) migrated from ``new_projer``;
- ``skills``: skill registry.

The previous non-event-driven engine (``agent.engine``), tool layer
(``tools``) and toy event bus (``evnts``) have been removed; ``runtime`` +
``bus`` supersede them.  The CLI / ``run_agent`` facade is not yet rewired to the
new runtime.

Importing this package has no side effects so the CLI can load it lazily.
"""
