"""``atom setup`` 的实现：初始化 ``config.yaml`` 与项目状态。

这是项目初始化的**唯一入口**：当前写入默认 ``config.yaml``（位于
``<项目根目录>/.agent/config.yaml``）；未来新增的初始化步骤（技能目录、默认工具等）
都追加到 :func:`run_setup` 里。

注意：**setup 不写快照** —— 快照只由 ``chat`` 更新（见 ``cli/snapshot.py``）。
"""

from __future__ import annotations

import sys


def run_setup(args) -> int:
    from atom_agent.config import (ConfigStore, default_config_yaml, empty_config,
                                   project_root)

    if getattr(args, "print_only", False):
        sys.stdout.write(default_config_yaml())
        return 0

    config_path = getattr(args, "config", None)
    store = ConfigStore(config_path)

    if store.exists() and not getattr(args, "force", False):
        print(f"配置已存在：{store.path}（使用 --force 覆盖）")
    else:
        # 未显式指定 --config 且新位置尚无配置时，迁移旧默认位置 `<项目根>/config.yaml`。
        legacy = project_root() / "config.yaml"
        migrated = False
        if config_path is None and not store.exists() and legacy.is_file() \
                and legacy.resolve() != store.path.resolve():
            try:
                store.save(ConfigStore(legacy).load(create=False))
                migrated = True
            except Exception:  # 旧文件读不了就退回默认配置
                migrated = False
        if not migrated:
            store.save(empty_config())
        if migrated:
            print(f"已迁移旧配置：{legacy} -> {store.path}")
        else:
            print(f"已写入默认配置：{store.path}")

    # 未来新增的初始化设置在这里追加（本项目所有初始化都收敛到 setup）。
    # 注意：setup 不写快照 —— 快照只由 chat 更新（见 cli/snapshot.py）。
    return 0
