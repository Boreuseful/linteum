"""Implementation of ``linteum model``: view / switch the default model (written back to ``config.yaml``)."""

from __future__ import annotations

import sys


def _store(args):
    from linteum_agent.config import ConfigStore

    return ConfigStore(getattr(args, "config", None))


def run_model(args) -> int:
    action = getattr(args, "model_action", None)
    if action == "show":
        return _show(args)
    if action == "set":
        return _set(args)
    print("Usage: linteum model {show,set} ...", file=sys.stderr)
    return 2


def _show(args) -> int:
    llm = _store(args).load(create=False).llm
    print(f"provider = {llm.provider}")
    print(f"model    = {llm.model}")
    return 0


def _set(args) -> int:
    from linteum_agent.config import llm_from_dict, llm_to_dict

    store = _store(args)
    config = store.load(create=True)
    data = llm_to_dict(config.llm)
    data["model"] = args.name
    if getattr(args, "provider", None):
        data["provider"] = args.provider

    try:
        llm = llm_from_dict(data)
    except Exception as exc:
        print(f"Setup failed: {exc}", file=sys.stderr)
        return 2

    config.set_llm(llm)
    store.save(config)
    # Note: model set does not write the snapshot -- the snapshot is only updated by chat (see cli/snapshot.py).
    print(f"provider = {llm.provider}")
    print(f"model    = {llm.model}")
    return 0
