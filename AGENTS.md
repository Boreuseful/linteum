# AGENTS.md

This repository is the `linteum_agent` Python library: an event-driven agent
runtime. Change the library only.

## Project map

- `linteum_agent/bus/` — Event envelope, RCU router, per-origin WFQ, WAL,
  artifacts, backpressure/coalescing, namespace guards, metrics.
- `linteum_agent/runtime/agent.py` — pure Agent reducer.
- `linteum_agent/runtime/state/` — AgentState, phase machine, checkpoints.
- `linteum_agent/runtime/context.py` — derived ContextPlan + deterministic digest.
- `linteum_agent/runtime/compaction.py` — deterministic context summary effect.
- `linteum_agent/storage/` — SQLite WAL/checkpoints, file artifacts, session history/context reads.
- `linteum_agent/runtime/tools/` — ToolSpec / Registry / Runtime / Sandbox / approval.
- `linteum_agent/runtime/plugins/` — capability plugins: manifest validation,
  two-phase load, quiesce/drain unload, lifecycle events, skill/mode sources.
- `linteum_agent/runtime/llm/` — canonical types, adapters, executor, router,
  breaker, cache, pricing.
- `linteum_agent/runtime/observability/` — run-log sidecar.

`bus` must not import `runtime`. `runtime` may import `bus`. Tools and adapters
are leaves: they must not reach into dispatch or state internals.

## Dev environment

- Python 3.12 (`.python-version`); uv project (`pyproject.toml`, `uv.lock`).
- `uv sync` — create `.venv` and install `anthropic`, `openai`, `pyyaml`.
- `uv run python -m compileall -q linteum_agent` — syntax/bytecode smoke check.
- `uv run python -c "import linteum_agent.bus, linteum_agent.runtime, linteum_agent.storage"` — import smoke check.
- SDK imports are deliberately lazy (`openai` / `anthropic` only inside adapter
  constructors). Keep it that way so bus/state/tool code can be imported without
  SDKs.

## Architecture rules (the bottom line)

These are not style preferences; they are the framework's invariants. If a
change requires breaking one, stop and redesign the change.

1. An `Event` is an immutable fact. Its payload must be pure JSON — no callables,
   sockets, file handles, clients, or bus references.
2. `origin` and `seq` are host/bus-owned. Never let a producer or plugin stamp
   them; plugin code can only publish `plugin.<id>.*`.
3. Publish order is fixed: externalise an oversized payload to an artifact,
   append to WAL, then enqueue for dispatch. Never dispatch before the WAL write.
4. The Agent is a pure reducer: no I/O, clock, random, or registry access. Its
   only side effect is publishing intent events (`llm.request`,
   `tool.call.requested`, `agent.completed`).
5. There is exactly one subscriber for each capability: `LLMExecutor` for
   `llm.request`, `ToolRuntime` for `tool.call.requested`. Do not add a second.
6. Tools and LLM adapters are leaves: they never publish, never subscribe, and
   never read `AgentState`.
7. Errors are data: convert failures to `*.failed` events with `error_info`.
   Never swallow `asyncio.CancelledError`; re-raise it after cleanup.
8. `*.delta` events are UI/streaming only; they must never enter
   `AgentState.messages`. Only final results and paired tool results do.
9. Every `tool.call.requested` must end in exactly one `tool.call.completed` or
   `tool.call.failed`. `phase == "idle"` implies `pending` is empty.
10. Per session, `seq` is strictly increasing with no gaps; `(session_id, seq)`
    is the ordering/idempotency key.
11. Context is a derived view with a deterministic `context.digest`; state holds
    references/summaries, full text stays in artifacts.
12. Large outputs are externalised before the result event references them.
13. A replay must never call a real provider, summariser, or retriever. If the
    recorded result is missing, report divergence; do not go to the network.
14. Provider differences belong in adapters only. Events and state must never
    contain provider-native fields.

## Where to make changes

- New event type: add it to `KNOWN_TYPES` in `bus/event.py`, confirm the namespace
  owner in `bus/namespace.py`, then add producer/subscriber. Update
  `bus/__init__.py` only if it is public API.
- New tool: define a `ToolSpec` (or use the decorator), register it in a
  `ToolRegistry` at assembly time. Do not pass a callable through the bus.
- New provider: subclass `BaseAdapter`, implement `capabilities`, `build_body`,
  and `astream`/`complete`; register through `AdapterRegistry`.
- New effect/subscriber: wire it in the host/bootstrap and publish result events.
  Do not call it directly from the Agent.
- New plugin capability field: extend `runtime/plugins/manifest.py` validation and
  `PluginHost` materialisation; keep `PluginContext` limited to
  `provide_tool`/`on_unload`/`log` (no bus/registry/state handles).
- State change: every new field must be JSON-serialisable, round-trip through
  `AgentState.to_dict/from_dict`, and be covered by `invariant_violations()`.
  Bump checkpoint compatibility if old snapshots cannot load.
- Bus change: preserve the `publish` step order in `bus/bus.py`; do not reorder
  dedup, WAL, or enqueue without redesigning the failure semantics.
- Logging: use `bus.add_log_sink` or `bus.on_any`; a log sink must never publish
  or raise into the bus.

## Style

- Python 3.12, `from __future__ import annotations`, type hints, dataclasses.
- Match the neighboring module; keep the existing English docstrings and `§`
  design references.
- Prefer `dataclass(frozen=True, slots=True)` for value objects; keep payloads
  plain JSON types.
- Add no third-party dependency without updating `pyproject.toml` and
  regenerating `uv.lock`.
- Use the existing error classes and `error_info`; do not invent string-only
  error paths.

## Known hazards (verify before building on them)

- `Agent.restore` is now async and closes an interrupted half-round; the host
  must still supply the same model/tool/context configuration before subscribing.
- Subagents: `SubagentOrchestrator` is the sole `subagent.spawn.requested`
  subscriber. Child sessions never acquire a lease (`lease_owned=False`), and
  `open_session`/`ensure_session` reject `kind="subagent"`; the root lease
  covers its child tree. Recovery/replay never creates a child or calls a
  provider, and a half-recorded spawn is paired as interrupted. The configured
  `agent.subagents.tool_name` is reserved at assembly and is never registered
  in a child's tool table (`max_depth=1`).
- `bus.publish` marks the event deduped and assigns `seq` before the WAL append
  (`bus/bus.py`). A WAL failure can drop a retry or leave a `seq` gap. On a
  reopened store the bus seeds `seq`/dedup from the WAL before the first publish.
- `WorkerPool` allows 4 concurrent handlers per session
  (`bus/scheduler/pool.py:20-34`); reducer serialisation and tool parallelism are
  not separated.
- Capability plugins (`runtime/plugins`) are implemented for tools/skills/modes;
  hooks, adapters, transport plugins, plugin KV and remote install remain
  unimplemented. Plugins run **in-process** and are trusted like the host.
  `agent.plugins.allow` is the authorization boundary; an empty allowlist never
  imports plugin code. Loading a plugin does not change already-open tool
  tables (next `session.mode.set` or new session); unloading quiesces, drains
  in-flight calls, then revokes. Lifecycle events use the reserved per-process
  `session_id="host:<uuid>"` partition, so clients must not create `host` /
  `host:*` sessions. Replay/restore never import plugins or call `setup`.
- Persistent plugin management (`plugin.enable`/`plugin.disable`) is off unless
  `gateway.plugins.manage=true`; the flag is captured when `GatewayServer`
  starts. The commands only rewrite `agent.plugins.allow` through
  `ConfigService.set` — never hot-load/unload, never restart, never import plugin
  code. An allowlist supplied by an environment override is rejected with
  `E_PLUGIN_CONFIG`. The TUI gates `Ctrl+E`/`Ctrl+D` on both the
  `plugins.manage` feature and `plugin.list.manage`.
- Cross-session memory, retrieval/RAG and CQRS projections beyond the session
  catalogue are not implemented yet. Do not assume their APIs exist.

## PR / commit

- Run the compile/import smoke checks above before committing.
- Commit style from history: `feat(bus): ...`, `fix(runtime): ...`,
  `refactor!: ...`, `docs: ...`.
- Keep commits scoped. Never commit `.venv`, `__pycache__`, `.kilo/`,
  `config.yaml`, or generated `dist/` artifacts.
- If a public export changes, update `bus/__init__.py` or `runtime/__init__.py`.
- Only commit when asked.
