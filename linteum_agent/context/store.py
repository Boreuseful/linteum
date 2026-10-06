from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from threading import RLock
from typing import Any, Iterable, Mapping

try:
    from ..messages.agent_message import Message, MessageInput, MessageInputs, normalize_messages
    from .session import Session
except ImportError:  # 支持直接将 /workspace/linteum_agent 加入 sys.path
    from messages.agent_message import Message, MessageInput, MessageInputs, normalize_messages
    from session import Session


class ContextStore:
    """按 session 保存 ``Session``（工作区 + 上下文表示 + 工具/Skill 配置 + usage），并提供 JSON 持久化。

    持久化格式为::

        {
          "version": 4,
          "sessions": {
            "demo-session": {
              "session_id": "demo-session",
              "workspace": "/workspace",
              "metadata": {"owner": "alice"},
              "tools": {"registered": ["read_file"], "disabled": ["bash"]},
              "skills": {"registered": ["python-code-review"]},
              "usage": {"input_tokens": 150, "output_tokens": 30,
                        "cache_hit_tokens": 45, "total_tokens": 180},
              "created_at": "2026-01-01T00:00:00+00:00",
              "updated_at": "2026-01-01T00:00:00+00:00",
              "messages": [
                {
                  "round": 1,
                  "context": [
                    {"role": "user", "content": "你好"},
                    {"role": "assistant", "content": "你好，有什么可以帮助你？"}
                  ],
                  "usage": {
                    "input_tokens": 12, "output_tokens": 8,
                    "cache_hit_tokens": 0, "total_tokens": 20,
                    "context_tokens": 20, "context_chars": 42,
                    "created_at": "2026-01-01T00:00:00+00:00",
                    "updated_at": "2026-01-01T00:00:05+00:00"
                  }
                }
              ]
            }
          }
        }

    ``messages`` 是**分轮的列表**：每一轮（= 一次完整 agent-loop）被包装成
    ``{round, context, usage}``，其中 ``context`` 是这一轮的全部消息（请求 + 每次 LLM
    调用 + 工具结果），``usage`` 保存这一轮的输入/输出 token 汇总、本轮占用的上下文空间
    （``context_tokens`` / ``context_chars``）以及本轮起止时间
    （``created_at`` / ``updated_at``）。

    ``tools`` / ``skills`` / ``usage`` 是每个会话的顶层字段，与 ``metadata`` 平级。

    向后兼容：

    - ``version=3``：``messages`` 是扁平消息列表（``tools`` / ``skills`` / ``usage``
      已是独立字段），读取时按「以 user 消息开始」切分成多轮；
    - ``version=2``：``tools`` / ``skills`` / ``usage`` 嵌在 ``metadata`` 中，读取时
      自动迁移到独立字段；
    - ``version=1``：``sessions`` 直接是消息列表，加载后会话的 ``workspace`` 取决于
      store 的 ``default_workspace``（未配置时为 ``None``，运行时继承工具注册表的
      工作区）。

    保存时统一写成最新格式（version 4），因此读取一次旧文件即可完成格式升级。

    不使用 pickle，生成的内容可以直接检查、迁移或由其他服务读取。
    """

    VERSION = 4
    LEGACY_VERSION = 1
    # version 2：工具/Skill 配置与 usage 曾嵌在 ``metadata`` 中。
    METADATA_VERSION = 2
    # version 3：``messages`` 是扁平消息列表（尚未按轮包装）。
    MESSAGES_VERSION = 3
    READABLE_VERSIONS = (VERSION, MESSAGES_VERSION, METADATA_VERSION, LEGACY_VERSION)

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

    def loads(self, value: str | bytes, *, merge: bool = False) -> None:
        payload = json.loads(value)
        if not isinstance(payload, dict):
            raise ValueError("上下文 JSON 根节点必须是对象")
        version = payload.get("version", self.LEGACY_VERSION)
        if version not in self.READABLE_VERSIONS:
            raise ValueError(f"不支持的上下文版本: {version}")
        sessions = payload.get("sessions", {})
        if not isinstance(sessions, dict):
            raise ValueError("上下文 JSON 的 sessions 必须是对象")
        if merge:
            self.merge(sessions)
        else:
            self.replace_all(sessions)

    def merge(self, sessions: Mapping[str, Any]) -> None:
        """把快照里的会话**合并**进当前 store：同 id 覆盖，其它会话保留。

        与 ``replace_all()`` 的区别：后者会丢弃 store 中已有的会话，前者不会。
        ``AgentRunner`` 载入某个会话时用本方法，避免把其它会话的记忆一起清掉。
        """
        with self._lock:
            for session_id, value in sessions.items():
                key = str(session_id)
                self._sessions[key] = self._coerce_session(key, value)

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

    def save_session(self, path: str | Path, session_id: str) -> None:
        """只把 ``session_id`` 这一个会话写入文件（一份文件 = 一个会话）。

        与 ``save()`` 的区别：后者写出 store 中的**全部**会话。按
        ``.agent/sessions/<session_id>.json`` 的命名约定，调用方通常只需要
        当前会话，因此不会把其它会话（例如刚构造出来的空会话）带进这个文件。
        """
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                raise KeyError(f"会话不存在: {session_id}")
            payload = {
                "version": self.VERSION,
                "sessions": {session_id: session.to_dict()},
            }
        Path(path).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def load(self, path: str | Path, *, merge: bool = False) -> None:
        """从文件读取会话；``merge=True`` 时保留 store 中已有的其它会话。"""
        self.loads(Path(path).read_text(encoding="utf-8"), merge=merge)


__all__ = ["ContextStore", "Session"]
