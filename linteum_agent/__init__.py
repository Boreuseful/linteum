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
- ``skills``: skill registry;
- ``tools``: tool registry and built-in tools;
- ``agent``: the agent engine that ties everything together.

Importing this package has no side effects so the CLI can load it lazily.
"""
