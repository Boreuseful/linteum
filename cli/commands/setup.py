"""``linteum setup`` 的实现：初始化 ``config.yaml`` 与用户级状态。

这是初始化的**唯一入口**：当前写入默认 ``config.yaml``（位于用户主目录
``~/.agent/config.yaml``）；未来新增的初始化步骤（技能目录、默认工具等）
都追加到 :func:`run_setup` 里。

注意：**setup 不写快照** —— 快照只由 ``chat`` 更新（见 ``cli/snapshot.py``）。
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
        print(f"配置已存在：{store.path}（使用 --force 覆盖）")
    else:
        # 未显式指定 --config 且新位置（~/.agent/config.yaml）尚无配置时，迁移旧默认位置：
        # 先 `<项目根>/.agent/config.yaml`（上一版默认位置），再 `<项目根>/config.yaml`（更早）。
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
                except Exception:  # 旧文件读不了就尝试下一个 / 退回默认配置
                    continue
        if migrated_from is None:
            store.save(empty_config())
            print(f"已写入默认配置：{store.path}")
        else:
            print(f"已迁移旧配置：{migrated_from} -> {store.path}")

    # 未来新增的初始化设置在这里追加（本项目所有初始化都收敛到 setup）。
    # 注意：setup 不写快照 —— 快照只由 chat 更新（见 cli/snapshot.py）。
    return 0
