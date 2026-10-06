"""当前会话（current session）指针。

`atom sessions new/switch` 会把选中的会话 ID 写入 ``~/.agent/current_session``；
`atom chat` 在未显式给 ``-s/--session`` 时**默认沿用**该会话。

- 默认位置：``~/.agent/current_session``（一个只含会话 ID 的文本文件）；
- 环境变量 ``ATOM_AGENT_CURRENT_SESSION`` 可覆盖文件路径（测试 / 多项目）；
- 读写都容忍文件缺失：读不到返回 ``None``（表示「未设置当前会话」），绝不抛异常。
"""

from __future__ import annotations

import os
from pathlib import Path

#: 当前会话文件名（位于用户级 ``~/.agent``）。
CURRENT_SESSION_FILENAME = "current_session"
#: 环境变量：覆盖当前会话文件路径。
CURRENT_SESSION_PATH_ENV = "ATOM_AGENT_CURRENT_SESSION"


def current_session_path() -> Path:
    """当前会话文件路径（环境变量 ``ATOM_AGENT_CURRENT_SESSION`` 可覆盖）。"""
    override = os.getenv(CURRENT_SESSION_PATH_ENV)
    if override and override.strip():
        return Path(override).expanduser()
    from cli.snapshot import agent_dir

    return agent_dir() / CURRENT_SESSION_FILENAME


def get_current_session() -> str | None:
    """读取当前会话 ID；未设置 / 文件不可读时返回 ``None``。"""
    try:
        text = current_session_path().read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return text or None


def set_current_session(session_id: str) -> Path:
    """把 ``session_id`` 设为当前会话，返回写入文件路径。"""
    session_id = str(session_id).strip()
    if not session_id:
        raise ValueError("session_id 不能为空")
    path = current_session_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(session_id + "\n", encoding="utf-8")
    return path


def clear_current_session() -> bool:
    """清除当前会话；返回是否真的删掉了文件。"""
    try:
        current_session_path().unlink()
        return True
    except OSError:
        return False
