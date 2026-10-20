# linteum-tui

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

## Common options

| Option | Description |
|---|---|
| `--spawn` | Start `linteum serve --stdio` as a child (default) |
| `--connect <URL>` | Attach to a running gateway, e.g. `ws://127.0.0.1:8765/` |
| `--linteum-bin <PATH>` | Python executable used in spawn mode |
| `--config <PATH>` | Python config passed to the spawned gateway |
| `--no-lease` | Pass `--no-lease` to the spawned gateway |
| `--workspace <DIR>` | Workspace for a new session |
| `--session <ID>` | Attach to an existing session |
| `--continue` | Attach to the most recently updated session |
| `--new` | Open the new-session dialog |
| `--profile <NAME>` | Profile passed to `session.create` |
| `--tui-config <PATH>` | Explicit TUI preferences file |
| `--theme <dark\|light\|mono>` | Start theme |
| `--no-color` | Force the monochrome theme |
| `--debug` | Verbose metadata logging |

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
