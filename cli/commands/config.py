"""``linteum config`` 的实现：读写 ``config.yaml``。"""

from __future__ import annotations

import json
import sys

_MISSING = object()


def _store(args):
    from linteum_agent.config import ConfigStore

    return ConfigStore(getattr(args, "config", None))


def run_config(args) -> int:
    action = getattr(args, "config_action", None)
    if action == "path":
        print(_store(args).path)
        return 0
    if action == "show":
        return _show(args)
    if action == "get":
        return _get(args)
    if action == "set":
        return _set(args)
    print("用法：linteum config {path,show,get,set} ...", file=sys.stderr)
    return 2


def _show(args) -> int:
    from linteum_agent.config import to_yaml

    config = _store(args).load(create=False)
    sys.stdout.write(to_yaml(config))
    return 0


def _dig(data, dotted: str):
    node = data
    for part in dotted.split("."):
        if isinstance(node, dict) and part in node:
            node = node[part]
        else:
            return _MISSING
    return node


def _get(args) -> int:
    config = _store(args).load(create=False)
    value = _dig(config.to_dict(), args.key)
    if value is _MISSING:
        print(f"没有这个配置项：{args.key}", file=sys.stderr)
        return 1
    if isinstance(value, (dict, list)):
        print(json.dumps(value, ensure_ascii=False, indent=2, default=str))
    else:
        print(value)
    return 0


def _set(args) -> int:
    from linteum_agent.config import llm_from_dict, llm_to_dict

    parts = args.key.split(".")
    if len(parts) != 2 or parts[0] != "llm":
        print("目前仅支持 config set llm.<field> <value>", file=sys.stderr)
        return 2

    field = parts[1]
    store = _store(args)
    config = store.load(create=True)
    data = llm_to_dict(config.llm)
    if field not in data:
        print(f"未知的 llm 字段：{field}；可选：{', '.join(data)}", file=sys.stderr)
        return 2

    try:
        data[field] = args.value  # llm_from_dict 会按字段类型规范化 / 校验
        llm = llm_from_dict(data)
    except Exception as exc:
        print(f"设置失败：{exc}", file=sys.stderr)
        return 2

    config.set_llm(llm)
    store.save(config)
    # 注意：config set 不写快照 —— 快照只由 chat 更新（见 cli/snapshot.py）。
    print(f"{args.key} = {getattr(llm, field)!r}")
    return 0
