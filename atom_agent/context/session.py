"""会话状态：对话的工作区、上下文表示和元数据。

Context 层用 ``Session`` 表示一次会话的全部状态：

```text
Session
├── session_id      会话 ID
├── workspace       会话工作区（工具执行的根目录）
├── context         上下文表示（provider-neutral 的消息列表）
└── metadata        会话级扩展信息
```

``atom_agent.tools.RuntimeContext`` 是 Session 的近似实现：它只有
``workspace``/``metadata``/``approve``/``cancelled``，且属于工具注册表级别，
不区分 ``session_id``。``Session.runtime_context()`` 负责把会话状态转换成
``RuntimeContext``，让工具在会话的工作区中执行，同时保留会话级元数据。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Iterable, Mapping

try:
    from ..messages.agent_message import (
        Message,
        MessageInput,
        MessageInputs,
        normalize_messages,
        to_dict as message_to_dict,
    )
except ImportError:  # 支持直接将 /workspace/atom_agent 加入 sys.path
    from messages.agent_message import (
        Message,
        MessageInput,
        MessageInputs,
        normalize_messages,
        to_dict as message_to_dict,
    )

if TYPE_CHECKING:  # 仅用于类型标注，避免 context 层与 tools 层在导入期互相依赖
    from ..tools.core import RuntimeContext

ApproveFn = Callable[[str, dict[str, Any]], bool]
CancelFn = Callable[[], bool]


def utc_now() -> str:
    """返回 UTC ISO8601 时间戳，用于会话创建/更新时间。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Session:
    """一次会话的完整状态：工作区 + 上下文表示 + 元数据。

    ``workspace`` 为 ``None`` 表示“尚未显式指定”，运行时可以继承工具注册表的
    工作区；``effective_workspace`` 给出实际生效的目录。``approve``/``cancelled``
    与 ``tools.RuntimeContext`` 语义一致，用来构造工具运行环境。
    """

    session_id: str = "default"
    workspace: Path | None = None
    context: list[Message] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    approve: ApproveFn | None = None
    cancelled: CancelFn | None = None

    def __post_init__(self) -> None:
        self.session_id = str(self.session_id or "default")
        if self.workspace is not None:
            self.workspace = Path(self.workspace).expanduser().resolve()
        self.context = normalize_messages(self.context)
        self.metadata = dict(self.metadata)

    # ------------------------------------------------------------------
    # 上下文表示
    # ------------------------------------------------------------------

    def messages(self) -> list[Message]:
        """返回上下文表示的副本，修改返回值不会影响 Session。"""
        return list(self.context)

    def append(self, messages: MessageInput | Iterable[MessageInput]) -> "Session":
        """追加消息（消息对象、字典或它们的集合）。"""
        self.context.extend(normalize_messages(messages))
        return self.touch()

    def replace(self, messages: MessageInputs) -> "Session":
        """整体替换上下文表示。"""
        self.context = normalize_messages(messages)
        return self.touch()

    def clear(self) -> "Session":
        """清空上下文表示（保留工作区和元数据）。"""
        self.context = []
        return self.touch()

    # ------------------------------------------------------------------
    # 会话信息
    # ------------------------------------------------------------------

    def touch(self) -> "Session":
        """刷新 ``updated_at``。"""
        self.updated_at = utc_now()
        return self

    def set_workspace(self, workspace: str | Path | None) -> "Session":
        """设置会话工作区；``None`` 表示恢复为“运行时继承”。"""
        self.workspace = None if workspace is None else Path(workspace).expanduser().resolve()
        return self.touch()

    @property
    def effective_workspace(self) -> Path:
        """实际生效的工作区；未显式指定时回退到当前进程工作目录。"""
        return self.workspace or Path.cwd()

    def update_metadata(self, metadata: Mapping[str, Any] | None = None, **values: Any) -> "Session":
        """合并会话元数据。"""
        self.metadata.update(dict(metadata or {}))
        self.metadata.update(values)
        return self.touch()

    def runtime_context(
        self,
        *,
        metadata: Mapping[str, Any] | None = None,
        approve: ApproveFn | None = None,
        cancelled: CancelFn | None = None,
    ) -> "RuntimeContext":
        """把会话转换成工具执行环境（``tools.RuntimeContext``）。

        元数据优先级：调用方 ``metadata`` < 会话 ``metadata``，并且始终写入当前
        ``session_id``；``approve``/``cancelled`` 缺省时使用会话自身的回调。
        """
        try:
            from ..tools.core import RuntimeContext
        except ImportError:  # 支持直接将 /workspace/atom_agent 加入 sys.path
            from tools.core import RuntimeContext

        merged = dict(metadata or {})
        merged.update(self.metadata)
        merged["session_id"] = self.session_id
        return RuntimeContext(
            workspace=self.effective_workspace,
            metadata=merged,
            approve=approve or self.approve,
            cancelled=cancelled or self.cancelled,
        )

    # ------------------------------------------------------------------
    # 序列化
    # ------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """转换为可 JSON 序列化的会话快照。"""
        return {
            "session_id": self.session_id,
            "workspace": str(self.workspace) if self.workspace is not None else None,
            "metadata": dict(self.metadata),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "messages": [message_to_dict(message) for message in self.context],
        }

    @classmethod
    def from_dict(cls, session_id: str, payload: Any) -> "Session":
        """从 ``to_dict()`` 结果恢复会话。

        同时兼容 version 1 旧格式：``payload`` 直接是消息列表，此时 ``workspace``
        为 ``None``（运行时继承工具注册表的工作区）。
        """
        if isinstance(payload, Session):
            return payload if payload.session_id == session_id else replace(payload, session_id=session_id)
        if isinstance(payload, (list, tuple)):
            return cls(session_id=session_id, context=list(payload))
        if not isinstance(payload, dict):
            raise ValueError(f"会话数据必须是对象或消息列表: {type(payload).__name__}")
        workspace = payload.get("workspace")
        return cls(
            session_id=str(payload.get("session_id") or session_id),
            workspace=None if workspace is None else Path(workspace),
            context=list(payload.get("messages") or []),
            metadata=dict(payload.get("metadata") or {}),
            created_at=payload.get("created_at") or utc_now(),
            updated_at=payload.get("updated_at") or utc_now(),
        )


__all__ = ["Session", "ApproveFn", "CancelFn", "utc_now"]

