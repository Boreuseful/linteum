"""快照：记录「模型配置 + config + 工作区设置」，默认启用。

语义：

- **默认位置**：``<项目根目录>/.agent/snapshot.json``（与被改到 ``.agent`` 的 config.yaml 同处）；
- **默认行为**：每条命令分发前解析工作区，并把当前「模型配置 + config + 工作区」写回快照
  （``--no-snapshot`` 关闭）；
- **工作区**：默认 = 命令启动目录（``Path.cwd()``）；``--use-snapshot`` 时改用快照里保存的
  工作区；
- ``setup`` / ``snapshot`` 子命令自行管理快照，``boot()`` 会跳过自动写入。

本模块属于 CLI（只读 ``atom_agent.config``），不修改 Agent 业务逻辑。
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

#: 快照文件格式版本。
SNAPSHOT_VERSION = 1
#: 快照文件名（位于 ``<项目根目录>/.agent``）。
SNAPSHOT_FILENAME = "snapshot.json"
#: 环境变量：覆盖快照文件路径（便于测试 / 多项目）。
SNAPSHOT_PATH_ENV = "ATOM_AGENT_SNAPSHOT"

#: 自行管理快照、跳过 ``boot()`` 自动写入的子命令。
_SELF_MANAGED_COMMANDS = frozenset({"setup", "snapshot"})


def agent_dir() -> Path:
    """项目级状态目录：``<项目根目录>/.agent``。"""
    from atom_agent.config import project_root
    from atom_agent.config.store import AGENT_DIRNAME

    return project_root() / AGENT_DIRNAME


def snapshot_path() -> Path:
    """快照文件路径（环境变量 ``ATOM_AGENT_SNAPSHOT`` 可覆盖）。"""
    override = os.getenv(SNAPSHOT_PATH_ENV)
    if override and override.strip():
        return Path(override).expanduser()
    return agent_dir() / SNAPSHOT_FILENAME


def load_snapshot() -> dict[str, Any] | None:
    """读取快照（不存在 / 损坏 / 非对象时返回 ``None``，绝不抛异常）。"""
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
    """把「模型配置 + config + 工作区」原子写入快照，返回写入路径。"""
    from atom_agent.config import ConfigStore, llm_to_dict

    config = ConfigStore(config_path).load(create=False)
    llm = config.llm
    target = Path(path).expanduser() if path is not None else snapshot_path()
    payload = {
        "version": SNAPSHOT_VERSION,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "workspace": str(Path(workspace).expanduser().resolve()),
        "model": llm_to_dict(llm),      # 模型配置（provider / model / 采样参数等）
        "config": config.to_dict(),     # 完整 config
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
    """删除快照；返回是否真的删掉了文件。"""
    try:
        snapshot_path().unlink()
        return True
    except OSError:
        return False


def resolve_workspace(args) -> Path:
    """工作区：默认 = 命令启动目录；``--use-snapshot`` 时用快照保存的工作区。"""
    if getattr(args, "use_snapshot", False):
        saved = (load_snapshot() or {}).get("workspace")
        if saved:
            return Path(str(saved)).expanduser()
    workspace = getattr(args, "workspace", None)
    return Path(workspace).expanduser() if workspace else Path.cwd()


def boot(args) -> None:
    """分发前统一处理快照：解析工作区 + 写快照（默认启用；失败不阻止命令）。"""
    if getattr(args, "command", None) in _SELF_MANAGED_COMMANDS:
        return
    workspace = resolve_workspace(args)
    # 把解析后的工作区回写到 namespace，供各 handler 复用（默认已是启动目录）。
    args.workspace = str(workspace)
    if getattr(args, "snapshot", True):
        try:
            save_snapshot(workspace, config_path=getattr(args, "config", None))
        except Exception:  # 快照失败绝不阻止命令执行
            pass


def refresh(args) -> None:
    """配置 / 模型被修改后重写快照，保持快照与 config 同步（失败静默）。"""
    if not getattr(args, "snapshot", True):
        return
    try:
        save_snapshot(resolve_workspace(args), config_path=getattr(args, "config", None))
    except Exception:
        pass
