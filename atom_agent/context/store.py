from __future__ import annotations

import json
from pathlib import Path
from threading import RLock
from typing import Any, Iterable

try:
    from ..messages.agent_message import Message, MessageInputs, MessageInput, normalize_messages, to_dict
except ImportError:  # 支持直接将 /workspace/atom_agent 加入 sys.path
    from messages.agent_message import Message, MessageInputs, MessageInput, normalize_messages, to_dict


class ContextStore:
    """按 session 保存反序列化后的消息，并提供 JSON 持久化能力。

    持久化格式为 ``{"version": 1, "sessions": {session_id: [messages]}}``，
    不使用 pickle，生成的内容可以直接检查、迁移或由其他服务读取。
    """

    VERSION = 1

    def __init__(self, initial: dict[str, MessageInputs] | None = None):
        self._sessions: dict[str, list[Message]] = {}
        self._lock = RLock()
        for session_id, messages in (initial or {}).items():
            self._sessions[session_id] = normalize_messages(messages)

    def get(self, session_id: str = "default") -> list[Message]:
        with self._lock:
            return list(self._sessions.get(session_id, []))

    def replace(self, session_id: str, messages: MessageInputs) -> None:
        with self._lock:
            self._sessions[session_id] = normalize_messages(messages)

    def append(self, session_id: str, messages: MessageInput | Iterable[MessageInput]) -> None:
        with self._lock:
            self._sessions.setdefault(session_id, []).extend(normalize_messages(messages))

    def clear(self, session_id: str = "default") -> None:
        with self._lock:
            self._sessions.pop(session_id, None)

    def clear_all(self) -> None:
        with self._lock:
            self._sessions.clear()

    def sessions(self) -> list[str]:
        with self._lock:
            return list(self._sessions)

    def to_dict(self) -> dict[str, Any]:
        with self._lock:
            return {
                "version": self.VERSION,
                "sessions": {
                    session_id: [to_dict(message) for message in messages]
                    for session_id, messages in self._sessions.items()
                },
            }

    def dumps(self, *, indent: int | None = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)

    def loads(self, value: str | bytes) -> None:
        payload = json.loads(value)
        if not isinstance(payload, dict):
            raise ValueError("上下文 JSON 根节点必须是对象")
        version = payload.get("version", 1)
        if version != self.VERSION:
            raise ValueError(f"不支持的上下文版本: {version}")
        sessions = payload.get("sessions", {})
        if not isinstance(sessions, dict):
            raise ValueError("上下文 JSON 的 sessions 必须是对象")
        self.replace_all({session_id: messages for session_id, messages in sessions.items()})

    def replace_all(self, sessions: dict[str, MessageInputs]) -> None:
        with self._lock:
            self._sessions = {
                session_id: normalize_messages(messages)
                for session_id, messages in sessions.items()
            }

    def save(self, path: str | Path) -> None:
        Path(path).write_text(self.dumps(), encoding="utf-8")

    def load(self, path: str | Path) -> None:
        self.loads(Path(path).read_text(encoding="utf-8"))