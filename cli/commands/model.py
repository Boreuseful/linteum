"""``atom model`` 的实现：查看 / 切换默认模型（写回 ``config.yaml``）。"""

from __future__ import annotations

import sys


def _store(args):
    from atom_agent.config import ConfigStore

    return ConfigStore(getattr(args, "config", None))


def run_model(args) -> int:
    action = getattr(args, "model_action", None)
    if action == "show":
        return _show(args)
    if action == "set":
        return _set(args)
    print("用法：atom model {show,set} ...", file=sys.stderr)
    return 2


def _show(args) -> int:
    llm = _store(args).load(create=False).llm
    print(f"provider = {llm.provider}")
    print(f"model    = {llm.model}")
    return 0


def _set(args) -> int:
    from atom_agent.config import llm_from_dict, llm_to_dict

    store = _store(args)
    config = store.load(create=True)
    data = llm_to_dict(config.llm)
    data["model"] = args.name
    if getattr(args, "provider", None):
        data["provider"] = args.provider

    try:
        llm = llm_from_dict(data)
    except Exception as exc:
        print(f"设置失败：{exc}", file=sys.stderr)
        return 2

    config.set_llm(llm)
    store.save(config)
    from cli import snapshot

    snapshot.refresh(args)  # 让快照与新的 config 同步
    print(f"provider = {llm.provider}")
    print(f"model    = {llm.model}")
    return 0
