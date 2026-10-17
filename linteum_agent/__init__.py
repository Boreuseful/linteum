"""The linteum agent library: an event-driven agent runtime.

Module layout:

- ``bus``: the event bus (immutable ``Event`` envelope, RCU router, per-origin
  fair scheduling, WAL durability, backpressure/coalescing, namespace isolation,
  artifact offload, metrics);
- ``runtime``: the event-driven runtime built on the bus — pure ``Agent`` reducer,
  state / phase machine / checkpoints, context plan, tool runtime + sandbox, the
  LLM layer (canonical types, provider adapters, executor) and the structured
  run-log sidecar.

Both packages were migrated from the experimental ``new_projer`` framework.  The
previous non-event-driven layers (``run_agent`` / ``agent.engine`` / ``tools`` /
``evnts`` / ``config`` / ``context`` / ``model`` / ``prompt`` / ``skills``) and
the ``cli`` package have been removed; a new host entry point will be built on
top of ``bus`` + ``runtime``.

Importing this package has no side effects so hosts can load submodules lazily.
"""
