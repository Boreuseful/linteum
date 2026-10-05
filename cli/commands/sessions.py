"""``atom sessions`` 的实现：列出 / 查看 / 创建 / 切换会话（不构造 Agent）。"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def _workspace(args) -> str:
    workspace = getattr(args, "workspace", None)
    return str(Path(workspace).expanduser()) if workspace else str(Path.cwd())


def run_sessions(args) -> int:
    action = getattr(args, "sessions_action", None)
    if action == "list":
        return _list(args)
    if action == "show":
        return _show(args)
    if action == "new":
        return _new(args)
    if action == "switch":
        return _switch(args)
    if action == "current":
        return _current(args)
    print("用法：atom sessions {list,show,new,switch,current} ...", file=sys.stderr)
    return 2


def _list(args) -> int:
    from atom_agent.run_agent import list_sessions

    infos = list_sessions(
        _workspace(args),
        include_project=not bool(getattr(args, "workspace_only", False)),
        only_current_workspace=bool(getattr(args, "current_workspace", False)),
    )
    if getattr(args, "json", False):
        print(json.dumps([info.to_dict() for info in infos], ensure_ascii=False, indent=2,
                         default=str))
        return 0
    if not infos:
        print("（没有找到任何会话）")
        return 0
    for info in infos:
        workspace = str(info.workspace) if info.workspace is not None else "-"
        print(
            f"{info.session_id}  [{info.scope}]  "
            f"msgs={info.message_count} rounds={info.round_count}  "
            f"updated={info.updated_at}  workspace={workspace}"
        )
    return 0


def _resolve_session(workspace: str, needle: str):
    """按 ID 或唯一前缀解析会话；返回 ``SessionInfo`` / 前缀歧义列表 / ``None``。"""
    from atom_agent.run_agent import list_sessions

    infos = list_sessions(workspace)
    for info in infos:
        if info.session_id == needle:
            return info
    prefix = [info for info in infos if info.session_id.startswith(needle)]
    if len(prefix) == 1:
        return prefix[0]
    if len(prefix) > 1:
        return prefix
    return None


def _show(args) -> int:
    from atom_agent.context import ContextStore

    workspace = _workspace(args)
    needle = args.session_id
    found = _resolve_session(workspace, needle)
    if found is None:
        print(f"未找到会话：{needle}", file=sys.stderr)
        return 1
    if isinstance(found, list):
        print(f"前缀 '{needle}' 匹配到多个会话：", file=sys.stderr)
        for info in found:
            print(f"  {info.session_id}", file=sys.stderr)
        return 2

    store = ContextStore(workspace=workspace)
    store.load(found.path, merge=True)
    session = store.get_session(found.session_id)
    if session is None:
        print(f"无法读取会话：{found.session_id}", file=sys.stderr)
        return 1

    messages = session.messages()
    if getattr(args, "json", False):
        print(json.dumps(
            {
                "session_id": found.session_id,
                "path": str(found.path),
                "workspace": str(found.workspace) if found.workspace is not None else None,
                "usage": dict(session.usage),
                "messages": [_message_dict(message) for message in messages],
            },
            ensure_ascii=False, indent=2, default=str,
        ))
        return 0

    print(f"会话   : {found.session_id}")
    print(f"文件   : {found.path}")
    print(f"工作区 : {found.workspace if found.workspace is not None else '(继承当前工作区)'}")
    print(f"用量   : {dict(session.usage)}")
    print(f"消息   : {len(messages)}")
    print("-" * 60)
    for message in messages:
        _print_message(message)
    return 0


def _new(args) -> int:
    """创建新会话（默认一式两份写入 .agent/sessions），并设为当前会话。"""
    from atom_agent.run_agent import create_context_json, new_session_id

    session_id = getattr(args, "session_id", None) or new_session_id()
    paths = create_context_json(
        session_id, _workspace(args), overwrite=bool(getattr(args, "force", False)))

    switched = bool(getattr(args, "switch", True))
    if switched:
        from cli.session_state import set_current_session

        set_current_session(session_id)

    if getattr(args, "json", False):
        print(json.dumps(
            {"session_id": session_id, "paths": [str(p) for p in paths], "switched": switched},
            ensure_ascii=False, indent=2, default=str))
        return 0
    print(f"新会话   : {session_id}")
    for path in paths:
        print(f"文件     : {path}")
    print(f"当前会话 : {'已切换' if switched else '未切换（--no-switch）'}")
    return 0


def _switch(args) -> int:
    """把已存在的会话设为当前会话（支持唯一前缀）。"""
    from cli.session_state import set_current_session

    workspace = _workspace(args)
    needle = args.session_id
    found = _resolve_session(workspace, needle)
    if found is None:
        print(f"未找到会话：{needle}（可先 `atom sessions new --id {needle}` 创建）",
              file=sys.stderr)
        return 1
    if isinstance(found, list):
        print(f"前缀 '{needle}' 匹配到多个会话：", file=sys.stderr)
        for info in found:
            print(f"  {info.session_id}", file=sys.stderr)
        return 2

    set_current_session(found.session_id)
    print(f"当前会话 : {found.session_id}")
    print(f"文件     : {found.path}")
    return 0


def _current(args) -> int:
    """显示当前会话（未设置时给出提示，不算错误）。"""
    from cli.session_state import current_session_path, get_current_session

    session_id = get_current_session()
    if session_id is None:
        if getattr(args, "json", False):
            print(json.dumps({"session_id": None}, ensure_ascii=False))
        else:
            print("（尚未设置当前会话；用 `atom sessions new` 新建，"
                  "或 `atom sessions switch <id>` 切换）")
        return 0

    found = _resolve_session(_workspace(args), session_id)
    path = None if (found is None or isinstance(found, list)) else str(found.path)
    if getattr(args, "json", False):
        print(json.dumps(
            {"session_id": session_id, "path": path,
             "current_session_path": str(current_session_path())},
            ensure_ascii=False, indent=2))
        return 0
    print(f"当前会话 : {session_id}")
    if path is not None:
        print(f"文件     : {path}")
    return 0


def _message_dict(message) -> dict:
    return {
        "role": getattr(message, "role", None),
        "content": getattr(message, "content", None),
        "tool_calls": [getattr(call, "name", None) for call in getattr(message, "tool_calls", []) or []],
    }


def _print_message(message) -> None:
    role = getattr(message, "role", None) or "?"
    print(f"[{role}]")
    content = getattr(message, "content", None)
    if isinstance(content, str):
        if content:
            print(content)
    elif content is not None:
        print(json.dumps(content, ensure_ascii=False, indent=2, default=str))
    for call in getattr(message, "tool_calls", []) or []:
        print(f"  -> tool_call {getattr(call, 'name', call)}")
