#!/usr/bin/env bash
#
# linteum uninstaller.
#
# Removes the `linteum` uv tool. With --purge it also deletes the user state
# directory (~/.agent), which holds the config, sessions and CLI snapshot.
#
# Note: this does NOT touch any shell configuration file (e.g. the PATH entry
# that `install.sh` added via `uv tool update-shell`); those are left untouched.
#
# Usage:
#   ./uninstall.sh            # remove the command only (user state is kept)
#   ./uninstall.sh --purge    # also delete ~/.agent (config, sessions, snapshots)
#   ./uninstall.sh -h | --help
#
set -euo pipefail

PROJECT_NAME="linteum"
AGENT_HOME="${HOME}/.agent"

usage() {
    cat <<'EOF'
Usage: ./uninstall.sh [--purge]

Remove the `linteum` command-line tool.

  --purge         also delete the user state directory ~/.agent
                  (config, sessions and CLI snapshot); without it, user state is kept
  -h, --help      show this help
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
if command -v uv >/dev/null 2>&1; then
    echo "==> removing the '$PROJECT_NAME' uv tool (if present)"
    uv tool uninstall "$PROJECT_NAME" 2>/dev/null || echo "    (not installed as a uv tool; skipping)"
else
    echo "==> uv not found; skipping tool removal"
fi

# --- 2. optionally remove user state -----------------------------------
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
