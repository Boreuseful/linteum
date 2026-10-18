"""The linteum agent library: an event-driven agent runtime.

Module layout:

- ``bus``: the event bus (immutable ``Event`` envelope, RCU router, per-origin
  fair scheduling, WAL durability, backpressure/coalescing, namespace isolation,
  artifact offload, metrics);
- ``runtime``: the event-driven runtime built on the bus — pure ``Agent`` reducer,
  state / phase machine / checkpoints, context plan + compaction, tool runtime +
  sandbox, the LLM layer (canonical types, provider adapters, executor) and the
  structured run-log sidecar;
- ``storage``: durable session storage — SQLite WAL/checkpoints, a filesystem
  artifact store and the session history/context read model;
- ``config``: semi-permanent configuration (LLM, gateway, storage, logging, UI)
  with YAML persistence, environment overrides, validation and UI metadata.
  Per-conversation values such as ``workspace`` live in ``SessionOptions``, not
  in the config file.

The bus/runtime/storage packages were migrated from the experimental
``new_projer`` framework.  The previous non-event-driven layers (``run_agent`` /
``agent.engine`` / ``tools`` / ``evnts`` / the legacy ``config`` / ``context`` /
``model`` / ``prompt`` / ``skills``) and the legacy ``cli`` package have been
removed.  A new host entry point (Gateway, then CLI/TUI/Web) is being built on
top of ``bus`` + ``runtime`` + ``config``.

Importing this package has no side effects so hosts can load submodules lazily.
"""
