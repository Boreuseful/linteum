from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from threading import RLock
from typing import Any, Iterable, Mapping

try:
    from ..messages.agent_message import Message, MessageInput, MessageInputs, normalize_messages
    from .session import Session
except ImportError:  # 支持直接将 /workspace/atom_agent 加入 sys.path
    from messages.agent_message import Message, MessageInput, MessageInputs, normalize_messages
    from session import Session


class ContextStore:
    """按 session 保存 ``Session``（工作区 + 上下文表示 + 元数据），并提供 JSON 持久化。

    持久化格式为::

        {
          "version": 2,
          "sessions": {
            "demo-session": {
              "session_id": "demo-session",
              "workspace": "/workspace",
              "metadata": {},
              "created_at": "2026-01-01T00:00:00+00:00",
              "updated_at": "2026-01-01T00:00:00+00:00",
              "messages": [{"role": "user", "content": "你好"}]
            }
          }
        }

    ``version=1`` 的旧格式（``sessions`` 直接是消息列表）仍可读取：读取后这些会话的
    ``workspace`` 取决于 store 的 ``default_workspace``（未配置时为 ``None``，运行时
    继承工具注册表的工作区）。不使用 pickle，生成的内容可以直接检查、迁移或由其他
    服务读取。
    """

    VERSION = 2
    LEGACY_VERSION = 1

    def __init__(
        self,
        initial: dict[str, MessageInputs] | None = None,
        *,
        workspace: str | Path | None = None,
    ):
        self._sessions: dict[str, Session] = {}
        self._lock = RLock()
        # 新建会话时使用的默认工作区；None 表示“运行时继承”。
        self.default_workspace: Path | None = (
            None if workspace is None else Path(workspace).expanduser().resolve()
        )
        for session_id, messages in (initial or {}).items():
            self.ensure_session(session_id).replace(messages)

    # ------------------------------------------------------------------
    # Session 级别读写
    # ------------------------------------------------------------------

    def ensure_session(
        self,
        session_id: str = "default",
        *,
        workspace: str | Path | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> Session:
        """获取会话；不存在时创建。已存在的会话不会被覆盖。

        ``workspace`` 只在会话不存在、或会话尚未指定工作区时生效；修改已有会话的
        工作区请使用 ``set_workspace()``。
        """
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                session = Session(
                    session_id=session_id,
                    workspace=self.default_workspace if workspace is None else workspace,
                    metadata=dict(metadata or {}),
                )
                self._sessions[session_id] = session
                return session
            if workspace is not None and session.workspace is None:
                session.set_workspace(workspace)
            if metadata:
                session.update_metadata(metadata)
            return session

    def get_session(self, session_id: str = "default") -> Session | None:
        """返回会话对象；不存在时为 ``None``（不会创建会话）。"""
        with self._lock:
            return self._sessions.get(session_id)

    def set_workspace(self, session_id: str, workspace: str | Path | None) -> Session:
        """设置会话工作区；``None`` 表示恢复为“运行时继承”。"""
        with self._lock:
            return self.ensure_session(session_id).set_workspace(workspace)

    def workspace(self, session_id: str = "default") -> Path:
        """返回会话生效的工作区；会话不存在时回退到默认工作区或 ``Path.cwd()``。"""
        session = self.get_session(session_id)
        if session is not None:
            return session.effective_workspace
        return self.default_workspace or Path.cwd()

    # ------------------------------------------------------------------
    # 兼容旧接口：直接读写消息列表（等价于操作 session.context）
    # ------------------------------------------------------------------

    def get(self, session_id: str = "default") -> list[Message]:
        with self._lock:
            session = self._sessions.get(session_id)
            return session.messages() if session is not None else []

    def replace(self, session_id: str, messages: MessageInputs) -> None:
        with self._lock:
            self.ensure_session(session_id).replace(messages)

    def append(self, session_id: str, messages: MessageInput | Iterable[MessageInput]) -> None:
        with self._lock:
            self.ensure_session(session_id).append(messages)

    def clear(self, session_id: str = "default") -> None:
        with self._lock:
            self._sessions.pop(session_id, None)

    def clear_all(self) -> None:
        with self._lock:
            self._sessions.clear()

    def sessions(self) -> list[str]:
        with self._lock:
            return list(self._sessions)

    # ------------------------------------------------------------------
    # 持久化
    # ------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        with self._lock:
            return {
                "version": self.VERSION,
                "sessions": {
                    session_id: session.to_dict()
                    for session_id, session in self._sessions.items()
                },
            }

    def dumps(self, *, indent: int | None = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)

    def loads(self, value: str | bytes) -> None:
        payload = json.loads(value)
        if not isinstance(payload, dict):
            raise ValueError("上下文 JSON 根节点必须是对象")
        version = payload.get("version", self.LEGACY_VERSION)
        if version not in (self.VERSION, self.LEGACY_VERSION):
            raise ValueError(f"不支持的上下文版本: {version}")
        sessions = payload.get("sessions", {})
        if not isinstance(sessions, dict):
            raise ValueError("上下文 JSON 的 sessions 必须是对象")
        self.replace_all(sessions)

    def replace_all(self, sessions: Mapping[str, Any]) -> None:
        """原子替换全部会话。

        取值可以是 ``Session``、``to_dict()`` 的会话快照，或旧格式的消息集合。
        """
        with self._lock:
            self._sessions = {
                session_id: self._coerce_session(session_id, value)
                for session_id, value in sessions.items()
            }

    def _coerce_session(self, session_id: str, value: Any) -> Session:
        if isinstance(value, Session):
            return value if value.session_id == session_id else replace(value, session_id=session_id)
        if isinstance(value, dict) and "role" not in value:
            return Session.from_dict(session_id, value)
        return Session(
            session_id=session_id,
            workspace=self.default_workspace,
            context=normalize_messages(value),
        )

    def save(self, path: str | Path) -> None:
        Path(path).write_text(self.dumps(), encoding="utf-8")

    def load(self, path: str | Path) -> None:
        self.loads(Path(path).read_text(encoding="utf-8"))


__all__ = ["ContextStore", "Session"]
