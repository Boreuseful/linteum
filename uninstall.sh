#!/usr/bin/env bash
#
# linteum uninstaller.
#
# Removes the `linteum` uv tool and the `linteum-tui` binary that `install.sh`
# places next to it. With --purge it also deletes the user state directory
# (~/.linteum, or $LINTEUM_HOME), which holds the config, state DB, artifacts
# and leases.
#
# Note: this does NOT touch any shell configuration file (e.g. the PATH entry
# that `install.sh` added via `uv tool update-shell`) and does not uninstall
# Rust (installed via rustup); those are left untouched.
#
# Usage:
#   ./uninstall.sh            # remove the command only (user state is kept)
#   ./uninstall.sh --purge    # also delete ~/.linteum (config, state, artifacts)
#   ./uninstall.sh -h | --help
#
set -euo pipefail

PROJECT_NAME="linteum"
AGENT_HOME="${LINTEUM_HOME:-${HOME}/.linteum}"

usage() {
    cat <<'EOF'
Usage: ./uninstall.sh [--purge]

Remove the `linteum` command-line tool and the `linteum-tui` client.

  --purge         also delete the user state directory ~/.linteum
                  (config, state DB, artifacts and leases); without it, user state is kept
  -h, --help      show this help

Rust and the shell PATH configuration added by `install.sh` are left in place.
EOF
}

PURGE=0
for arg in "$@"; do
    case "$arg" in
        --purge)   PURGE=1 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "uninstall.sh: unknown option '$arg'" >&2; usage >&2; exit 2 ;;
    esac
done

# --- 1. remove the uv tool ---------------------------------------------
TOOL_BIN="$HOME/.local/bin"
if command -v uv >/dev/null 2>&1; then
    TOOL_BIN="$(uv tool dir --bin 2>/dev/null || printf '%s' "$HOME/.local/bin")"
    echo "==> removing the '$PROJECT_NAME' uv tool (if present)"
    uv tool uninstall "$PROJECT_NAME" 2>/dev/null || echo "    (not installed as a uv tool; skipping)"
else
    echo "==> uv not found; skipping tool removal"
fi

# --- 2. remove the TUI binary ------------------------------------------
TUI_BIN="$TOOL_BIN/linteum-tui"
if [ -e "$TUI_BIN" ]; then
    echo "==> removing the 'linteum-tui' binary: $TUI_BIN"
    rm -f -- "$TUI_BIN"
else
    echo "==> no linteum-tui binary at $TOOL_BIN"
fi

# --- 3. optionally remove user state -----------------------------------
if [ "$PURGE" -eq 1 ]; then
    if [ -d "$AGENT_HOME" ]; then
        echo "==> removing user state: $AGENT_HOME"
        rm -rf -- "$AGENT_HOME"
    else
        echo "==> no user state at $AGENT_HOME"
    fi
else
    echo "==> user state kept at $AGENT_HOME (use --purge to remove it)"
fi

echo
echo "==> done."
