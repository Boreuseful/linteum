# Linteum

Linteum is an event-driven AI agent runtime written in Python. Its core is a
pure reducer over immutable events: a WAL-backed bus orders and persists every
fact, while LLM providers and tools plug in as leaves that never touch the
reducer's state. The project ships two commands:

- `linteum` — the CLI: interactive and one-shot chat, a session catalogue,
  configuration management, and a gateway over stdio or WebSocket.
- `linteum-tui` — a standalone Rust terminal client that attaches to that
  gateway (or spawns a private one).

Every session has its own workspace, event log, and checkpointed state, so a
conversation can be resumed later.

Language: [中文](README_zh_CN.md)

## Requirements

| Component | Requirement | Notes |
|---|---|---|
| OS | Linux | The tested target; macOS and Windows have not been tested. |
| Python | 3.12+ | `uv` can provision a suitable interpreter automatically. |
| uv | Any recent version | Installed automatically by `install.sh` if missing. |
| curl | Any recent version | Used only when `install.sh` has to install uv or Rust. |
| Rust | stable toolchain | Only for `linteum-tui`; installed via rustup by `install.sh` when `cargo` is missing. Toolchain settings live in `linteum-tui/rust-toolchain.toml`. |
| websockets | `>=13` | Optional; required by `linteum serve --ws`. Install with `./install.sh --web`. |

## Install

```bash
git clone https://github.com/Boreuseful/linteum.git linteum
cd linteum

./install.sh                 # editable install + build/install linteum-tui
./install.sh --web           # also install websockets for `linteum serve --ws`
./install.sh --no-tui        # skip Rust and the TUI
./install.sh --no-editable   # install a snapshot instead of tracking this source tree
```

`install.sh` installs `uv` and Rust if they are missing, installs the Python
project as a uv tool, compiles the TUI with `cargo build --release`, and puts
both commands in the uv tool bin directory. It also adds that directory to your
shell configuration.

Verify the installation:

```bash
linteum --version
linteum-tui --version
```

If the commands are not on `PATH` in the current shell yet, open a new shell or
re-source your shell rc file.

## Quick start

```bash
linteum setup                              # choose provider/model/API key interactively
linteum chat -w /path/to/project           # interactive REPL
linteum chat -w /path/to/project -m "Summarize README.md"   # one-shot
linteum sessions                           # list saved sessions
```

Terminal client:

```bash
linteum-tui --spawn                        # start a private stdio gateway (default)
linteum-tui --connect ws://127.0.0.1:8765/ # attach to a running gateway
```

The default `--spawn` mode launches `linteum serve --stdio` as a child and
needs only the Python CLI. The `--connect` mode needs a WebSocket gateway:

```bash
./install.sh --web                         # one-time: install websockets
linteum serve --ws                         # ws://127.0.0.1:8765
```

Run `linteum-tui --help` for the full client option list.

In `--spawn` mode the session picker is filtered to the current directory, so
sessions created for other workspaces are hidden. Press `a` in the picker to
show all workspaces, or pass `--session <ID>`. `--continue` is not implemented
yet. See [`linteum-tui/README.md`](linteum-tui/README.md#sessions-and-workspaces)
for details.

## Configuration

The semi-permanent configuration lives in `~/.linteum/config.yaml`.
`LINTEUM_HOME` moves the whole state directory (`config.yaml`, `state.db`,
`artifacts/`, `logs/`); `LINTEUM_CONFIG` overrides just the config file path.
Run `linteum config path` to print the effective location.

`linteum setup` or `linteum config init` writes a commented template. The file
stores provider credentials, model profiles, context/tool policy, gateway
limits, storage paths, logging and UI preferences. The session `workspace` is
**not** configured here — it is supplied per conversation (`-w` /
`session.create`).

Commonly changed fields (omitted fields keep their defaults; run
`linteum config init` for the complete template):

```yaml
version: 1

agent:
  # The `default` profile, used when no profile is selected.
  llm:
    provider: openai              # openai | openai_compatible | anthropic | deepseek | moonshot | qwen | zhipu | siliconflow
    model: gpt-4o-mini
    openai_mode: responses        # responses | chat_completions; null = provider default
    api_key: ${OPENAI_API_KEY}    # ${ENV_VAR} reference, or a literal key (file becomes 0600)
    base_url: null                # required for openai_compatible and custom endpoints
    timeout: 60.0
    temperature: 0.7
    max_tokens: 4096
    top_p: null
    reasoning_effort: null        # e.g. low | medium | high
    thinking: null                # provider-specific toggle

  default_profile: default

  # Named profiles, selected with `linteum chat -p <name>`.
  profiles:
    fast:
      provider: deepseek
      model: deepseek-chat
      api_key: ${DEEPSEEK_API_KEY}
    claude:
      provider: anthropic
      model: claude-sonnet-5-5
      api_key: ${ANTHROPIC_API_KEY}

  # Tried in order after the selected profile fails.
  fallbacks:
    - provider: openai
      model: gpt-4o-mini
      api_key: ${OPENAI_API_KEY}

  context:
    policy: passthrough           # passthrough | compacting
    max_tokens: 120000            # used when policy=compacting
    compact_to_tokens: 80000
    keep_last_messages: 8
    max_summary_tokens: 4000

  tools:
    approval: ask                 # ask | allow | deny
    auto_approve: []              # names that skip the gate when approval=ask
    bash_timeout: 30.0

  skills:
    enabled: true
    project_dir: .linteum/skills  # relative to the session workspace

  subagents:
    enabled: false                # opt in; the root then sees tool_name
    tool_name: spawn_subagent     # reserved; never registered in child tables
    max_depth: 1                  # children cannot spawn subagents
    max_concurrent_per_session: 4
    max_queued_per_session: 32
    default_timeout_ms: 1800000   # 30 minutes
    max_timeout_ms: 3600000       # 60 minutes; longer requests are clamped
    cancel_grace_ms: 5000
    max_prompt_bytes: 16384       # whole spawn prompt, in UTF-8 bytes
    max_summary_bytes: 7168       # bounded summary returned to the parent
    progress_interval_ms: 1000
    default_tools: []             # empty = every parent tool except tool_name
    allowed_tools: []             # empty = the parent's live tool registry
    default_model_profile: default
    allowed_model_profiles: []    # empty = default_model_profile only
    approval_policy: allow        # allow | deny | inherit (agent.tools.approval)
    inherit_skills: true
    max_tokens: 0                 # per-child budget; 0 = unlimited
    max_usd: 0.0
    max_rounds: 0
    max_wall_ms: 0

  budget:                         # 0 = unlimited
    tokens: 0
    usd: 0.0
    wall_ms: 0
    rounds: 0

  session:
    input_policy: queue           # queue | interrupt | reject
    idle_ttl_ms: 1800000

gateway:
  transport: auto                 # auto | inproc | stdio | websocket
  websocket:
    host: 127.0.0.1               # non-loopback hosts require --allow-remote
    port: 8765

storage:
  state_db: ~/.linteum/state.db
  artifacts: ~/.linteum/artifacts
  checkpoints:
    keep: 3
    every_n_events: 500

logging:
  dir: ~/.linteum/logs
  file: run.log
  level: info                     # debug | info | warning | error
  format: text                    # text | json
  include_content: false          # write full payloads (metadata only by default)
  console: false

ui:
  language: zh-CN
  theme: auto                     # auto | light | dark
  show_reasoning: true
```

`agent.tools.auto_approve` lists tool names that run without human approval
while `agent.tools.approval` is `ask` (for example
`[write_file, edit_file]`); `deny` still refuses them.

Subagents are off by default. With `agent.subagents.enabled: true`, a root
session's tool table gains the reserved `spawn_subagent`; each spawn runs in an
isolated child session and returns a single bounded summary. The model may only
narrow `tools`, `model_profile`, and `timeout_ms`: an empty `allowed_tools`
falls back to the parent's live registry, and an empty `allowed_model_profiles`
allows only `default_model_profile`. Children cannot spawn further children, are
hidden from `session.list` unless `include_subagents=true`, and share the root
session's lease.

### Capability plugins

A plugin is a directory containing `plugin.yaml` under one of
`agent.plugins.dirs` (default `~/.linteum/plugins`). It can provide tools
(`capabilities.tools` + a Python `entry`), skill directories
(`capabilities.skills`) and mode files (`capabilities.modes`). Nothing is
imported until its id is listed in `agent.plugins.allow`:

```yaml
agent:
  plugins:
    dirs: ["~/.linteum/plugins"]
    allow: ["acme.search"]
```

```bash
linteum plugin list                 # discover/validate without importing code
linteum plugin show acme.search
linteum plugin validate ~/.linteum/plugins/acme-search
linteum plugin enable acme.search   # persist agent.plugins.allow (restart to load)
linteum plugin disable acme.search
```

`plugin enable/disable` only rewrites `agent.plugins.allow`; it never imports
plugin code and takes effect on the next host start. The CLI always allows it
because the local user owns the config. The gateway exposes the same commands to
clients only when `gateway.plugins.manage: true` (default `false`, restart
required); in `linteum-tui` open `/plugins`, select a row, and press `Ctrl+E` or
`Ctrl+D` followed by `y`. The command is refused when the allowlist comes from
an environment override, because writing the file would not change the effective
value.

> **Trust:** in-process plugin tools run with the host's full privileges. Only
> enable plugins you trust as much as the host itself. Plugins that provide only
> skills/modes do not execute code.

Plugin loads take effect for new sessions or after `session.mode.set`; they do
not mutate an already-open tool table. Unloading quiesces new calls, waits for
in-flight calls (`agent.plugins.drain_grace_ms`), then revokes the tools.

#### Config and secrets

Per-plugin configuration lives in `agent.plugins.config.<id>` (file-only: these
maps have no environment override) and is passed to `setup` as a read-only
snapshot. Values are plain JSON. Each secret name in
`agent.plugins.secrets.<id>.<NAME>` must be an `env:VAR` reference:

```yaml
agent:
  plugins:
    config:
      acme.search:
        region: eu-west
    secrets:
      acme.search:
        API_KEY: env:ACME_API_KEY   # the only accepted secret source
```

Config values are validated against the plugin's `config_schema` before its
code is imported; a missing required key or a wrong type fails the load with
`E_PLUGIN_CONFIG`. Secret values are never written to the config file: every
name in the manifest's `needs_secrets` must map to `env:VAR`, and the host
reads the variable at load time. `plugin.list` / `plugin show` (including their
`--json` forms) show each config key's source (`default` or `file`) and the
declared secret names only — never config values and never secret values.

#### Runtime management

With `gateway.plugins.manage: true`, clients on a local transport (embedded
inproc, stdio, or loopback WebSocket) may change the running plugin set:

- `plugin.set_allowed` (`ids`, `mode`) persists `agent.plugins.allow`. The
  default `mode: deferred` only rewrites the file and reports
  `restart_required`; an explicit `mode: apply` also loads/unloads the running
  set immediately.
- `plugin.load` / `plugin.unload` / `plugin.reload` act on the live set for one
  plugin id. Unloading drains in-flight calls and revokes tools; it does not
  edit the persisted allowlist.

Both the manage flag and a local transport are required; remote clients keep
read-only `plugin.list`. A runtime apply failure is reported per plugin and
never rolls back the already-persisted allowlist. The CLI's `plugin
enable/disable` stay offline and restart-scoped: they only rewrite
`agent.plugins.allow`, never import or hot-apply plugin code, and take effect
on the next host start. There are no CLI runtime load/unload commands.

A JSON client on a local loopback WebSocket can apply the already-saved
allowlist without restarting the gateway: `plugin.list` returns an additive
`allow` field (the file-level `agent.plugins.allow`, order and unknown ids
preserved; omitted when the config file is unreadable), and submitting it back
with `plugin.set_allowed(ids=<allow>, mode="apply")` loads/unloads the running
set to match. The TUI exposes the same flow as `Ctrl+A` in the `/plugins`
panel in attach mode, when the gateway advertises `plugins.runtime` and
`manage: true` and at least one row has saved changes (`restart_required`).
The apply submits the `allow` snapshot from the last `plugin.list`, so after
editing `agent.plugins.allow` externally (or from another client) press
`Ctrl+R` in the panel first. Applying does not restart the gateway: open
sessions see tool changes on the next `session.mode.set` or new session;
otherwise restart the gateway externally (spawn mode keeps `Ctrl+T`).
Non-loopback (remote) WebSocket clients are rejected by the local-transport
gate.

#### Hooks, subscriptions, and limits

Besides tools/skills/modes, a manifest may observe the run and publish its own
events. Hooks and subscriptions are read-only; declaring any of them requires
an `entry`:

```yaml
capabilities:
  hooks: [before_llm_request, after_llm_completed]
subscribes: [llm.completed, tool.call.completed]
publishes: [plugin.acme.search.*]
limits:
  max_events_per_sec: 100
  max_handler_ms: 2000
  max_concurrency: 4
  max_output_bytes: 65536
  max_consecutive_failures: 5
ordering:
  priority: 100
  fail_mode: closed
```

- `capabilities.hooks` accepts only `before_llm_request` and
  `after_llm_completed`. Hooks are observe-only: they never become bus
  subscribers and never enter `AgentState`.
- `subscribes` (top level) uses exact event types only — `llm.completed`,
  `llm.failed`, `tool.call.completed`, `tool.call.failed`, `agent.completed`,
  `agent.failed` — with no wildcards.
- `publishes` (top level) stays inside the plugin's own namespace:
  `plugin.<own-id>.*` or `plugin.<own-id>.<segment>` (a single lowercase
  segment).
- `limits` ranges are inclusive; defaults in parentheses:
  `max_events_per_sec` 1..10000 (100), `max_handler_ms` 1..60000 (2000),
  `max_concurrency` 1..64 (4), `max_output_bytes` 1024..262144 (65536),
  `max_consecutive_failures` 1..100 (5). Work above the rate limit or output
  cap is shed (logged, not dispatched), and repeated handler failures
  quarantine the plugin.
- `ordering`: `priority` -1000..1000 (100) orders hook handlers as
  `(priority, plugin_id, registration index)`; `fail_mode` is `closed`
  (default: a failure stops the remaining handlers) or `open` (continue).

Each block is a strict whitelist: unknown keys are rejected, an explicit
`null` inside `limits`/`ordering` is rejected, while a `null` block means "not
configured". Plugins run in-process and are trusted like the host.

Other sections and limits (`agent.skills` caps, `gateway.limits`,
`gateway.delta`, `gateway.history`, `gateway.backpressure`,
`storage.checkpoints`, `logging` rotation) are documented in the generated
template.

Built-in providers are `openai`, `openai_compatible`, `anthropic`, `deepseek`,
`moonshot`, `qwen`, `zhipu`, and `siliconflow`. Each provider has a default
endpoint and API-key environment variable (`OPENAI_API_KEY`,
`ANTHROPIC_API_KEY`, `DEEPSEEK_API_KEY`, `MOONSHOT_API_KEY`,
`DASHSCOPE_API_KEY`, `ZHIPU_API_KEY`, `SILICONFLOW_API_KEY`);
`openai_compatible` and unknown providers require `base_url`. Profiles that
share a provider must agree on `base_url`, `api_key`, and `openai_mode`.

Useful config commands:

```bash
linteum config path
linteum config list --show-origin --section agent
linteum config get agent.llm.model
linteum config set agent.llm.model gpt-4o-mini
linteum config set agent.llm.api_key '${OPENAI_API_KEY}'
linteum config set agent.profiles '{"fast":{"provider":"deepseek","model":"deepseek-chat"}}'
linteum config edit               # open $VISUAL/$EDITOR and validate on save
linteum config validate --effective
```

## Uninstall

```bash
./uninstall.sh            # remove linteum and linteum-tui; keep ~/.linteum
./uninstall.sh --purge    # also remove ~/.linteum (config, state, artifacts)
```

Rust (installed via rustup) and the shell PATH configuration added by
`install.sh` are left in place.
