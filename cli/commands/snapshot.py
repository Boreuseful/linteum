"""Implementation of ``linteum snapshot``: view / write / clear / locate the snapshot."""

from __future__ import annotations

import json
import sys


def run_snapshot(args) -> int:
    action = getattr(args, "snapshot_action", None)
    if action == "path":
        from cli.snapshot import snapshot_path

        print(snapshot_path())
        return 0
    if action == "show":
        return _show(args)
    if action == "save":
        return _save(args)
    if action == "clear":
        return _clear(args)
    print("Usage: linteum snapshot {show,save,clear,path} ...", file=sys.stderr)
    return 2


def _show(args) -> int:
    from cli.snapshot import load_snapshot, snapshot_path

    data = load_snapshot()
    if data is None:
        print(f"(no snapshot: {snapshot_path()})")
        return 0
    if getattr(args, "json", False):
        print(json.dumps(data, ensure_ascii=False, indent=2, default=str))
        return 0
    model = data.get("model") or {}
    print(f"snapshot : {snapshot_path()}")
    print(f"updated  : {data.get('updated_at')}")
    print(f"workspace: {data.get('workspace')}")
    print(f"model    : {model.get('provider')} / {model.get('model')}")
    return 0


def _save(args) -> int:
    from cli.snapshot import resolve_workspace, save_snapshot

    path = save_snapshot(resolve_workspace(args), config_path=getattr(args, "config", None))
    print(f"Snapshot written: {path}")
    return 0


def _clear(args) -> int:
    from cli.snapshot import clear_snapshot

    print("Snapshot deleted" if clear_snapshot() else "(no snapshot to delete)")
    return 0
