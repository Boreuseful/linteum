#!/usr/bin/env bash
#
# linteum installer.
#
# Installs the `linteum` command-line tool:
#   1. installs `uv` (https://docs.astral.sh/uv/) if it is not already available;
#   2. installs this project as a uv tool, exposing the `linteum` command;
#   3. makes sure the uv tool bin directory is on your PATH.
#
# Usage:
#   ./install.sh                 # editable install (default): the command tracks this source tree
#   ./install.sh --no-editable   # regular install: a snapshot copy of the current sources
#   ./install.sh --web           # also install the optional websockets transport
#   ./install.sh -h | --help
#
# Environment:
#   UV_INSTALL_DIR   directory for the `uv` binary when it has to be installed
#                    (default: $HOME/.local/bin)
#
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_NAME="linteum"

usage() {
    cat <<'EOF'
Usage: ./install.sh [--editable | --no-editable] [--web]

Install the `linteum` command-line tool.

  --editable      (default) editable install: the command tracks this source tree
  --no-editable   regular install: a snapshot copy of the current sources
  --web           also install `websockets` for `linteum serve --ws`
  -h, --help      show this help

Steps performed:
  1. install `uv` (https://docs.astral.sh/uv/) if it is not already available;
  2. install this project as a uv tool, exposing the `linteum` command;
  3. make sure the uv tool bin directory is on your PATH.

Environment:
  UV_INSTALL_DIR  directory for the `uv` binary when it has to be installed
                  (default: $HOME/.local/bin)
EOF
}

EDITABLE=1
WITH_WEB=0
for arg in "$@"; do
    case "$arg" in
        -e|--editable) EDITABLE=1 ;;
        --no-editable) EDITABLE=0 ;;
        --web)         WITH_WEB=1 ;;
        -h|--help)     usage; exit 0 ;;
        *) echo "install.sh: unknown option '$arg'" >&2; usage >&2; exit 2 ;;
    esac
done

# --- 1. make sure `uv` is available ------------------------------------
if command -v uv >/dev/null 2>&1; then
    echo "==> uv found: $(uv --version)"
else
    echo "==> uv not found; installing it from https://astral.sh/uv ..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    # The official installer drops uv into UV_INSTALL_DIR (default ~/.local/bin).
    export PATH="${UV_INSTALL_DIR:-$HOME/.local/bin}:$PATH"
fi

if ! command -v uv >/dev/null 2>&1; then
    echo "install.sh: uv is still not on PATH after installation." >&2
    echo "            Add its directory to PATH (e.g. \$HOME/.local/bin) and re-run." >&2
    exit 1
fi

# --- 2. install the project as a uv tool -------------------------------
echo "==> installing '$PROJECT_NAME' from $PROJECT_ROOT"
WEB_ARGS=()
if [ "$WITH_WEB" -eq 1 ]; then
    WEB_ARGS+=(--with websockets)
fi
if [ "$EDITABLE" -eq 1 ]; then
    uv tool install --force --editable "$PROJECT_ROOT" "${WEB_ARGS[@]}"
else
    uv tool install --force "$PROJECT_ROOT" "${WEB_ARGS[@]}"
fi

# --- 3. make sure the tool bin directory is on PATH (persisted) --------
# `uv tool update-shell` appends the directory to the shell configuration
# (e.g. ~/.bashrc, ~/.zshenv, ~/.config/fish/config.fish) so the `linteum`
# command is available in every new shell. It is idempotent: re-running it does
# not duplicate the entry (the reverse removal lives in uninstall.sh).
TOOL_BIN="$(uv tool dir --bin 2>/dev/null || printf '%s' "$HOME/.local/bin")"
echo "==> ensuring $TOOL_BIN is on your PATH (uv tool update-shell)"
uv tool update-shell || echo "    (could not update the shell configuration automatically)"

# --- done --------------------------------------------------------------
echo
echo "==> done."
echo "    '$TOOL_BIN' has been added to your shell configuration; open a new"
echo "    shell (or re-source your shell rc) to use 'linteum' globally."
echo "    Verify with: linteum --version"
echo "    Then try:    linteum setup"
echo "                 linteum config list"
echo "                 linteum chat -w /path/to/project"
if [ "$WITH_WEB" -eq 0 ]; then
    echo "    (websockets for 'linteum serve --ws' is optional: ./install.sh --web)"
fi
if ! command -v linteum >/dev/null 2>&1; then
    echo
    echo "note: not on this shell's PATH yet. For the current shell, run:"
    echo "    export PATH=\"$TOOL_BIN:\$PATH\""
fi

