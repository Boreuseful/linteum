"""Snapshot: records the "model config + config + workspace" used by chat, so chat need not be re-specified.

Semantics:

- **Default location**: ``~/.agent/snapshot.json`` (alongside ``~/.agent/config.yaml``);
- **Only ``chat`` writes the snapshot** (including bare ``linteum``): before each ``chat`` run it
  writes back the current "model config + config + workspace", so a later
  ``linteum --use-snapshot chat`` need not re-specify the workspace; ``--no-snapshot`` disables it;
- **Other commands are read-only**: ``status`` / ``sessions`` / ``config`` / ``model`` / ``tools`` /
  ``skills`` etc. never modify the snapshot; ``--use-snapshot`` is only used to **read** the workspace
  from the snapshot;
- ``setup`` / ``snapshot`` skip ``boot()``; ``snapshot save`` / ``snapshot clear`` are the only explicit
  management entry points.

This module belongs to the CLI (it only reads ``linteum_agent.config``) and does not change Agent
business logic.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

#: Snapshot file format version.
SNAPSHOT_VERSION = 1
#: Snapshot file name (located in the user-level ``~/.agent``).
SNAPSHOT_FILENAME = "snapshot.json"
#: Environment variable: overrides the snapshot file path (for tests / multiple projects).
SNAPSHOT_PATH_ENV = "linteum_agent_SNAPSHOT"

#: Subcommands that manage the snapshot themselves and skip ``boot()`` auto-writing.
_SELF_MANAGED_COMMANDS = frozenset({"setup", "snapshot"})
#: The **only** command that auto-writes the snapshot (bare ``linteum`` also falls to chat). All other commands are read-only.
_SNAPSHOT_WRITERS = frozenset({"chat"})


def agent_dir() -> Path:
    """User-level state directory: ``~/.agent`` (config / snapshot / current session and other user state all live here)."""
    from linteum_agent.config import agent_home

    return agent_home()


def snapshot_path() -> Path:
    """The snapshot file path (the ``linteum_agent_SNAPSHOT`` env var can override it)."""
    override = os.getenv(SNAPSHOT_PATH_ENV)
    if override and override.strip():
        return Path(override).expanduser()
    return agent_dir() / SNAPSHOT_FILENAME


def load_snapshot() -> dict[str, Any] | None:
    """Read the snapshot (returns ``None`` when missing / corrupted / not an object; never raises)."""
    try:
        raw = snapshot_path().read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def save_snapshot(workspace: str | Path, *, config_path: str | Path | None = None,
                  path: str | Path | None = None) -> Path:
    """Atomically write "model config + config + workspace" to the snapshot; return the written path."""
    from linteum_agent.config import ConfigStore, llm_to_dict

    config = ConfigStore(config_path).load(create=False)
    llm = config.llm
    target = Path(path).expanduser() if path is not None else snapshot_path()
    payload = {
        "version": SNAPSHOT_VERSION,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "workspace": str(Path(workspace).expanduser().resolve()),
        "model": llm_to_dict(llm),      # model config (provider / model / sampling params, etc.)
        "config": config.to_dict(),     # the full config
    }
    target.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n"

    fd, tmp = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return target


def clear_snapshot() -> bool:
    """Delete the snapshot; return whether the file was actually deleted."""
    try:
        snapshot_path().unlink()
        return True
    except OSError:
        return False


def resolve_workspace(args) -> Path:
    """Workspace: default = the command's start directory; with ``--use-snapshot`` use the workspace saved in the snapshot."""
    if getattr(args, "use_snapshot", False):
        saved = (load_snapshot() or {}).get("workspace")
        if saved:
            return Path(str(saved)).expanduser()
    workspace = getattr(args, "workspace", None)
    return Path(workspace).expanduser() if workspace else Path.cwd()


def boot(args) -> None:
    """Handle the snapshot before dispatch -- **only ``chat`` writes; all other commands are read-only**.

    - First resolve the workspace (read the snapshot when ``--use-snapshot``, otherwise use the
      command's start directory) and write it back to the namespace for handlers to reuse;
    - Only when the command is ``chat`` (including bare ``linteum``) and ``--snapshot`` is on does it
      write "model config + config + workspace" back to the snapshot; other commands (status /
      sessions / config / model / tools / skills ...) never write;
    - ``setup`` / ``snapshot`` manage this themselves and are skipped;
    - Any write failure is silent (the snapshot never blocks command execution).
    """
    if getattr(args, "command", None) in _SELF_MANAGED_COMMANDS:
        return

    workspace = resolve_workspace(args)      # --use-snapshot reads the snapshot only
    args.workspace = str(workspace)          # for handlers to reuse (already the start directory by default)

    command = getattr(args, "command", None) or "chat"
    if command not in _SNAPSHOT_WRITERS:
        return                               # not chat: never touch the snapshot
    if getattr(args, "snapshot", True):
        try:
            save_snapshot(workspace, config_path=getattr(args, "config", None))
        except Exception:  # a snapshot failure never blocks command execution
            pass
