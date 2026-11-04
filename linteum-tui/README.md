# linteum-tui

English | [简体中文](README_zh_CN.md)

Standalone Rust terminal client for `linteum_agent`. It talks to the gateway
over stdio or WebSocket and does not link or embed the Python runtime.

## Requirements

- Rust stable (the pinned toolchain is in `rust-toolchain.toml`)
- The Python `linteum` CLI on `PATH` for the default spawn mode

## Build

```bash
cargo build --release
```

The binary is written to `target/release/linteum-tui`.

## Run

Spawn a private gateway over stdio (the default):

```bash
cargo run --release -- --spawn

# or the built binary
./target/release/linteum-tui --spawn
```

Attach to a gateway that is already running:

```bash
# terminal 1: start the gateway
linteum serve --ws

# terminal 2: attach the TUI
./target/release/linteum-tui --connect ws://127.0.0.1:8765/
```

Run `linteum-tui --help` for the full option list.

## Sessions and workspaces

The session picker filters the catalogue by **workspace**, comparing the stored
workspace string exactly. In `--spawn` mode the filter defaults to the directory
you start the TUI from (or the explicit `--workspace` value); `--connect` adds
no filter unless `--workspace` is given. A session created for
`/path/to/project` is therefore hidden when you start `linteum-tui --spawn`
from another directory.

If the filtered list is empty, the TUI skips the picker and opens the
new-session dialog directly, **even when `--new` was not passed**. This is a
consequence of the empty filtered result, not of the `--new` flag: the initial
`session.list` request carries the current-directory filter, returns zero rows,
and the view model switches the overlay from the picker to the new-session
form.

To list every session, press `a` (all workspaces) or `r` (refresh) in the
picker. If you are already on the new-session form, press `Esc` and then `F2`
to reopen the picker, then `a`. To attach without the picker, pass
`--session <ID>`; `linteum sessions` prints the catalogue with ids and
workspaces.

`--continue` is accepted but not implemented yet: it does not attach to the
most recently updated session and leaves you at the picker.

## Command-line options

| Option | Description |
|---|---|
| `--spawn` | Start `linteum serve --stdio` as a child (the default mode). Conflicts with `--connect`. |
| `--connect <URL>` | Attach to a running gateway, e.g. `ws://127.0.0.1:8765/`. Conflicts with `--spawn`. |
| `--linteum-bin <PATH>` | `linteum` executable used in spawn mode. |
| `--config <PATH>` | Python config passed to `linteum serve --config` (spawn mode only). |
| `--no-lease` | Pass `--no-lease` to the spawned gateway (spawn mode only). |
| `--workspace <DIR>` | Workspace for a new session. In spawn mode it also becomes the picker filter. |
| `--session <ID>` | Attach to an existing session. Conflicts with `--continue` and `--new`. |
| `--continue` | Reserved; not implemented yet (leaves you at the picker). Conflicts with `--session` and `--new`. |
| `--new` | Open the new-session dialog at startup. Conflicts with `--session` and `--continue`. |
| `--profile <NAME>` | Profile passed to `session.create`. |
| `--tui-config <PATH>` | Explicit TUI preferences file (TOML). |
| `--theme <dark\|light\|mono>` | Start theme. |
| `--show-reasoning` | Expand reasoning sections at startup. |
| `--no-color` | Force the monochrome theme. |
| `--debug` | Verbose metadata logging. |
| `--debug-content` | Include payload content in logs. |
| `-h, --help` | Print the option list. |
| `-V, --version` | Print the version. |

The TUI resolves its settings from CLI flags, then environment variables, then
the TUI TOML file, then the Python `config.yaml`, then defaults:

| Environment variable | Meaning |
|---|---|
| `LINTEUM_HOME` | State directory (default `~/.linteum`): `config.yaml`, `state.db`, `artifacts/`, `logs/`. |
| `LINTEUM_CONFIG` | Python config path (default `$LINTEUM_HOME/config.yaml`). |
| `LINTEUM_BIN` | Path to the `linteum` CLI used in spawn mode. |
| `LINTEUM_TUI_CONFIG` | TUI preferences TOML (default `$XDG_CONFIG_HOME/linteum-tui/config.toml`). |
| `LINTEUM_TUI_MODE` | Startup mode: `spawn`, or `connect`/`attach`. |
| `LINTEUM_TUI_URL` | Gateway URL for attach mode. |
| `LINTEUM_TUI_THEME` | `dark`, `light`, `mono`, or `auto`. |
| `LINTEUM_TUI_SHOW_REASONING` | `1`/`true`/`yes`/`on` expands reasoning sections. |
| `NO_COLOR` | Forces the monochrome theme. |

## Keybindings

### Global

| Key | Action |
|---|---|
| `Enter` | Send the message (composer focus). |
| `Ctrl+J` | Insert a newline in the composer. |
| `Ctrl+C` | Interrupt the running agent; when idle, clear the composer. |
| `Ctrl+N` | Open the new-session dialog. |
| `Ctrl+P` | Open the command palette. |
| `Ctrl+R` | Toggle reasoning sections. |
| `Ctrl+G` | Resume a paused agent. |
| `Ctrl+Q` | Quit the TUI. |
| `F1` / `Alt+1` | Help. |
| `F2` / `Alt+2` | Session picker. |
| `F3` / `Alt+3` | Activity log. |
| `F4` / `Alt+4` | Approvals. |
| `F5` / `Alt+5` | Reconnect now. |
| `F6` / `Alt+6` | Subagents. |
| `Esc` | Close the current overlay. |
| `Tab` / `Shift+Tab` | Toggle focus between the composer and the sidebar. |
| `Left` / `Right` / `Up` / `Down` | Move the cursor (composer) or the selection (lists). |
| `Home` / `End` | Start/end of the composer line. |
| `Ctrl+Home` / `Ctrl+End` | Scroll the transcript to the top/bottom. |
| `PageUp` / `PageDown` | Scroll the transcript one page. |
| `Backspace` / `Delete` | Delete backward/forward. |

### Session picker (`F2`)

| Key | Action |
|---|---|
| `Up` / `Down` | Move the selection. |
| `Enter` | Attach to the selected session. |
| `n` | Open the new-session dialog. |
| `r` | Refresh the list. |
| `a` | Toggle all workspaces (clears or reapplies the workspace filter). |
| `m` | Load the next page. |
| `Esc` | Close the picker. |

### New-session dialog

| Key | Action |
|---|---|
| `Enter` | Create the session. |
| `Tab` / `Shift+Tab` / `Up` / `Down` | Switch between the workspace and title fields. |
| `Left` / `Right` / `Home` / `End` | Move the cursor. |
| `Backspace` / `Delete` | Edit the field. |
| printable characters | Type into the focused field. |
| `Esc` | Close the dialog. |

### Approval dialog

| Key | Action |
|---|---|
| `y` | Grant the pending tool call. |
| `n` | Deny the pending tool call. |
| `Esc` | Close the dialog. |

### Command palette (`Ctrl+P`)

| Key | Action |
|---|---|
| `Up` / `Down` | Move the selection. |
| `Enter` | Run the selected action. |
| printable characters | Filter the action list. |
| `Left` / `Right` / `Home` / `End` | Move the filter cursor. |
| `Backspace` / `Delete` | Edit the filter. |
| `Esc` | Close the palette. |

Palette actions: quit, toggle reasoning, sessions, activity, approvals,
sidebar subagents, subagents, help, new session, reconnect, resume, copy last
assistant message, open last artifact, expand/collapse last tool call.

### Subagents (`F6`)

| Key | Action |
|---|---|
| `Up` / `Down` | Move the selection. |
| `Enter` | Open the selected subagent's context. |
| `Esc` | Close. |

In the subagent context view: `Up`/`Down` scroll one line, `PageUp`/`PageDown`
scroll ten lines, `r` reloads the context, `Esc` returns to the subagent list.

### Workspace sidebar

The sidebar handles these keys only while it is visible and has focus.

| Key | Action |
|---|---|
| `Up` / `Down` | Previous/next entry. |
| `Home` / `End` | First/last entry. |
| `Left` / `Right` | Collapse/expand the selected folder. |
| `Enter` / `Space` | Toggle the selected folder. |
| `/` | Move focus to the composer and start a slash command. |
| `Tab` / `Shift+Tab` / `Esc` | Move focus back to the composer. |

### Slash commands

Type `/` in the composer to open the command menu. `Up`/`Down` select a
command, `Enter` runs it, `Tab` completes it, and `Esc` closes the menu.

| Command | Action |
|---|---|
| `/files` | Show the workspace tree. |
| `/sidebar` | Show or hide the sidebar. |
| `/merge` | Merge the sidebar into one block. |
| `/split` | Split the sidebar into files, preview, and subagents. |
| `/collapse` | Collapse the selected folder. |
| `/expand` | Expand the selected folder. |
| `/preview` | Show the preview panel. |
| `/info` | Show the info panel. |
| `/subagents` | Show subagent status in the sidebar. |
| `/refresh` | Reload the workspace tree. |
| `/exit` | Leave the current session. |
| `/quit` | Exit `linteum-tui`. |

## Test

```bash
cargo test
```

Integration tests that need a real gateway are ignored by default:

```bash
cargo test --test integration_stdio -- --ignored
cargo test --test integration_ws -- --ignored
```

`scripts/check.sh` runs formatting, clippy, the unit tests, and the ignored
integration tests (the latter need the Python `linteum` CLI).
