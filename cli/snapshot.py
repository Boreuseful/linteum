"""快照：记录 chat 用到的「模型配置 + config + 工作区」，让聊天不必反复指定。

语义：

- **默认位置**：``<项目根目录>/.agent/snapshot.json``（与被改到 ``.agent`` 的 config.yaml 同处）；
- **只有 ``chat`` 写快照**（含裸 ``atom``）：每次 ``chat`` 运行前把当前「模型配置 + config +
  工作区」写回，使后续 ``atom --use-snapshot chat`` 无需重复指定工作区；``--no-snapshot`` 关闭；
- **其它命令只读**：``status`` / ``sessions`` / ``config`` / ``model`` / ``tools`` / ``skills``
  等绝不改动快照；``--use-snapshot`` 仅用于**读取**快照里的工作区；
- ``setup`` / ``snapshot`` 跳过 ``boot()``；``snapshot save`` / ``snapshot clear`` 是唯一的
  显式管理入口。

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
#: **唯一**会自动写快照的命令（裸 ``atom`` 也落到 chat）。其余命令一律只读。
_SNAPSHOT_WRITERS = frozenset({"chat"})


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
    """分发前处理快照 —— **只有 ``chat`` 写，其它命令只读**。

    - 先解析工作区（``--use-snapshot`` 时读快照，否则用命令启动目录）并回写到 namespace，
      供各 handler 复用；
    - 仅当命令是 ``chat``（含裸 ``atom``）且 ``--snapshot`` 开启时，才把「模型配置 + config +
      工作区」写回快照；其余命令（status / sessions / config / model / tools / skills …）绝不写；
    - ``setup`` / ``snapshot`` 自行管理，直接跳过；
    - 任何写入失败都静默（快照绝不阻止命令执行）。
    """
    if getattr(args, "command", None) in _SELF_MANAGED_COMMANDS:
        return

    workspace = resolve_workspace(args)      # --use-snapshot 只读快照
    args.workspace = str(workspace)          # 供各 handler 复用（默认已是启动目录）

    command = getattr(args, "command", None) or "chat"
    if command not in _SNAPSHOT_WRITERS:
        return                               # 非 chat：绝不改快照
    if getattr(args, "snapshot", True):
        try:
            save_snapshot(workspace, config_path=getattr(args, "config", None))
        except Exception:  # 快照失败绝不阻止命令执行
            pass
