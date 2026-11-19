# linteum-web

English | [简体中文](README_zh_CN.md)

Standalone browser client for `linteum_agent`. It speaks Gateway Protocol v1
over WebSocket and does not import or embed the Python runtime. CLI, TUI and Web
differ only in transport and renderer.

## Requirements

- Node.js **22 LTS (>= 22.12)** or 20.19+; npm >= 10.
- A running gateway:
  ```bash
  uv run linteum serve --ws        # ws://127.0.0.1:8765/
  ```
- The `websockets` package in the gateway environment (`linteum[web]`).

## Build

```bash
npm install
npm run build
```

The bundle is written to `dist/`; `npm run preview` serves it locally. The build
runs `tsc --noEmit` before Vite emits the bundle.

## Run

```bash
# terminal 1 — gateway
uv run linteum serve --ws

# terminal 2 — web client
npm run dev                        # http://127.0.0.1:5173/
```

The client connects to `ws://127.0.0.1:8765/` by default. Override the URL with
`VITE_GATEWAY_URL` or edit it on the connection screen; the last URL is
remembered in `localStorage`.

| Environment variable | Meaning |
|---|---|
| `VITE_GATEWAY_URL` | Gateway WebSocket URL (default `ws://127.0.0.1:8765/`). |

## Sessions and workspaces

The New Session dialog asks for a workspace and defaults to the last workspace
used in this browser, then `/workspace`. Each session keeps its own workspace
and profile; the session list shows the gateway catalogue.

When the gateway advertises `config.profiles`, the dialog can pick a named
profile and defaults to `agent.default_profile`. Reconnects resubscribe from the
last durable `seq` and replay the session's history.

## Settings

The Settings page is gateway-wide and does not need an open session. Its sidebar
groups the pages by owner:

- **Client** — General (language), Appearance (theme), Connection.
- **Agent** — LLM, Plugins, and the `agent.*` schema sections (context, tools,
  background, skills, subagents, steering, budget, session defaults, modes,
  plugin settings).
- **Gateway / Storage / Logging** — the remaining schema sections.

Behaviour worth knowing:

- **LLM** edits named profiles, the default profile and the fallback chain
  through `config.profile.*` / `config.fallback.*`. It needs the `config`
  feature (and `config.profiles` for named profiles) plus
  `gateway.config.manage=true`; writes are accepted on a local transport only.
  Secrets stay masked and an untouched masked key is never written back.
- **Plugins** lists the runtime catalogue and toggles enable/disable. It needs
  `gateway.plugins.manage=true`. With `plugins.runtime`, applying the edited
  allow list (`plugin.set_allowed`) re-assembles plugins in place; otherwise the
  change takes effect after a gateway restart. The runtime apply commands are
  local-transport only; `plugin.enable` / `plugin.disable` just persist the
  allow list.
- **Schema sections** need `config.settings`. They support global search, source
  filters, restore-defaults (section and per-field) and a `persist=false` diff
  preview. Scalars and scalar lists are editable; maps, objects and
  lists-of-objects stay read-only; `${VAR}` references are preserved through
  `raw_value`; environment-owned fields are read-only.
- **Configuration push** (`config.push`): after an in-process configuration
  write the gateway sends an ephemeral `config.changed` with the new revision
  and changed `{key, scope}` pairs (never values). Idle clients refresh their
  loaded sections automatically; a client with an unsaved draft shows a stale
  banner and disables Save until it explicitly reloads. `logging.*` changes
  apply live, without restarting the gateway.

## Workspace panel (demo bridge)

A browser cannot read the gateway host's filesystem, and Gateway Protocol v1
has no `workspace.*` command. This demo therefore ships a **demo-only**
read-only bridge in `vite.config.ts`:

- `GET /demo-api/workspace/tree?root=...&path=...`
- `GET /demo-api/workspace/files?root=...`
- `GET /demo-api/workspace/file?root=...&path=...`
- `GET /demo-api/workspace/raw?root=...&path=...` (raw bytes with HTTP
  byte-range/`206` support for the browser-native PDF viewer; 32 MB cap instead
  of the 256 KB text cap)

It is `apply: "serve"` only, reads at most 256 KB per text file, resolves
`realpath` and rejects paths outside the session workspace. Swap the provider in
`src/workspace/index.ts` when a real `workspace.tree` / `workspace.read`
protocol arrives; the panel itself stays untouched.

## Keyboard shortcuts

The UI is mouse-first; these are accelerators, not the only path.

| Keys | Action |
|---|---|
| `Enter` | Send the message |
| `Shift+Enter` | Insert a new line |
| `/` | Open the slash-command menu |
| `Ctrl/Cmd+K` | Command palette |
| `Ctrl/Cmd+N` | New session |
| `F1` | Help |
| `F2` | Session picker |
| `y` / `n` | Approve / deny the pending tool call |
| `Esc` | Close the current dialog; on Settings, go back to chat |

In the slash menu, `Up`/`Down` move the selection, `Enter`/`Tab` run the command,
and `Esc` closes the menu. In SVG previews, `+`/`-`/`0` zoom and drag pans.

## Slash commands

Type `/` in the composer to open the command menu.

| Command | Action |
|---|---|
| `/new` | New session |
| `/files` | Open the workspace panel |
| `/sidebar` | Toggle the right rail |
| `/preview` | Workspace preview |
| `/info` | Info panel |
| `/subagents` | Subagents panel |
| `/plugins` | Plugins panel |
| `/refresh` | Refresh the session list |
| `/merge` | Stack panels |
| `/split` | Split panels into tabs |
| `/collapse` | Collapse the active panel |
| `/expand` | Expand the active panel |
| `/stop` | Stop the running agent |
| `/leave` | Leave the current session |
| `/help` | Keyboard-shortcut help |
| `/quit` | Disconnect from the gateway |

