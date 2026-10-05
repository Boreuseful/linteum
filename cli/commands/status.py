"""``atom status`` 的实现：打印运行时状态（离线，不构造 Agent）。"""

from __future__ import annotations

import json
from pathlib import Path


def run_status(args) -> int:
    from atom_agent.config import ConfigStore, project_root
    from atom_agent.run_agent import list_sessions
    from cli.session_state import get_current_session
    from cli.snapshot import load_snapshot, snapshot_path

    workspace = Path(getattr(args, "workspace", None) or Path.cwd()).expanduser()
    store = ConfigStore(getattr(args, "config", None))
    exists = store.exists()
    llm = store.load(create=False).llm
    snap = load_snapshot()

    payload = {
        "workspace": str(workspace),
        "project_root": str(project_root()),
        "config_path": str(store.path),
        "config_exists": exists,
        "provider": llm.provider,
        "model": llm.model,
        "snapshot_path": str(snapshot_path()),
        "has_snapshot": snap is not None,
        "current_session": get_current_session(),
        "session_count": len(list_sessions(str(workspace))),
    }

    if getattr(args, "json", False):
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
        return 0

    print(f"workspace    : {payload['workspace']}")
    print(f"project_root : {payload['project_root']}")
    config_note = "" if exists else "（尚未创建）"
    print(f"config       : {payload['config_path']}{config_note}")
    print(f"provider     : {payload['provider']}")
    print(f"model        : {payload['model']}")
    snap_note = "" if payload["has_snapshot"] else "（尚无快照）"
    print(f"snapshot     : {payload['snapshot_path']}{snap_note}")
    print(f"session      : {payload['current_session'] or '(未设置当前会话)'}")
    print(f"sessions     : {payload['session_count']}")
    return 0
