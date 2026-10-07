"""Implementation of ``linteum setup``: initialize ``config.yaml`` and user-level state.

This is the **sole entry point** for initialization: it currently writes the default ``config.yaml``
(located in the user home directory ``~/.agent/config.yaml``); future initialization steps (skill
directories, default tools, etc.) are all appended to :func:`run_setup`.

Note: **setup does not write the snapshot** -- the snapshot is only updated by ``chat`` (see
``cli/snapshot.py``).
"""

from __future__ import annotations

import sys


def run_setup(args) -> int:
    from linteum_agent.config import (AGENT_DIRNAME, ConfigStore, default_config_yaml,
                                   empty_config, project_root)

    if getattr(args, "print_only", False):
        sys.stdout.write(default_config_yaml())
        return 0

    config_path = getattr(args, "config", None)
    store = ConfigStore(config_path)

    if store.exists() and not getattr(args, "force", False):
        print(f"Config already exists: {store.path} (use --force to overwrite)")
    else:
        # When --config is not explicitly given and the new location (~/.agent/config.yaml) has no
        # config yet, migrate from the old default locations: first `<project root>/.agent/config.yaml`
        # (the previous default), then `<project root>/config.yaml` (even earlier).
        migrated_from = None
        if config_path is None and not store.exists():
            legacy_candidates = [
                project_root() / AGENT_DIRNAME / "config.yaml",
                project_root() / "config.yaml",
            ]
            for legacy in legacy_candidates:
                if not legacy.is_file() or legacy.resolve() == store.path.resolve():
                    continue
                try:
                    store.save(ConfigStore(legacy).load(create=False))
                    migrated_from = legacy
                    break
                except Exception:  # if the old file cannot be read, try the next one / fall back to the default config
                    continue
        if migrated_from is None:
            store.save(empty_config())
            print(f"Wrote default config: {store.path}")
        else:
            print(f"Migrated old config: {migrated_from} -> {store.path}")

    # Future initialization settings are appended here (all initialization in this project converges
    # on setup).
    # Note: setup does not write the snapshot -- the snapshot is only updated by chat (see cli/snapshot.py).
    return 0
