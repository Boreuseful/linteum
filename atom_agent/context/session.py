"""会话状态：对话的工作区、上下文表示、元数据和工具执行配置。

Context 层用 ``Session`` 表示一次会话的全部状态：

```text
Session
├── session_id      会话 ID
├── workspace       会话工作区（工具执行的根目录）
├── context         上下文表示（provider-neutral 的消息列表）
├── metadata        会话级扩展信息
│   └── tools       工具配置（启用/禁用、每个工具的选项）
├── approve         工具授权回调
└── cancelled       取消检查回调
```

工具执行上下文（原来的 ``atom_agent.tools.RuntimeContext``）已经合并进
``Session``：``workspace`` / ``metadata`` / ``approve`` / ``cancelled`` /
``log()`` / ``is_cancelled()`` 全部由 Session 提供，``ToolRegistry`` 直接读取
Session 中的工具配置。``tools.RuntimeContext`` 名字仍然可用，但它只是
``Session`` 的兼容别名。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

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

ApproveFn = Callable[[str, dict[str, Any]], bool]
CancelFn = Callable[[], bool]

# 工具配置在 metadata 中的键：metadata["tools"]。
TOOL_CONFIG_KEY = "tools"


def utc_now() -> str:
    """返回 UTC ISO8601 时间戳，用于会话创建/更新时间。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Session:
    """一次会话的完整状态：工作区 + 上下文表示 + 元数据 + 工具执行配置。

    它同时就是工具运行上下文（原 ``tools.RuntimeContext`` 已合并到这里）：
    ``workspace`` 为 ``None`` 表示“尚未显式指定”，运行时可以继承工具注册表的
    工作区，``effective_workspace`` 给出实际生效的目录；``metadata`` /
    ``approve`` / ``cancelled`` / ``log()`` / ``is_cancelled()`` 供
    ``ToolRegistry`` 和工具使用，其中 ``metadata["tools"]`` 保存工具配置。
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

    # ------------------------------------------------------------------
    # 工具执行环境（原 tools.RuntimeContext 的能力）
    # ------------------------------------------------------------------

    @classmethod
    def cwd(cls, session_id: str = "default") -> "Session":
        """以当前进程工作目录作为会话工作区。"""
        return cls(session_id=session_id, workspace=Path.cwd())

    def log(self, message: str) -> None:
        """调用 ``metadata["log"]`` 回调（工具输出通道）。"""
        callback = self.metadata.get("log")
        if callable(callback):
            callback(message)

    def is_cancelled(self) -> bool:
        """``cancelled`` 回调为真时表示会话已请求取消。"""
        return bool(self.cancelled and self.cancelled())

    def tool_config(self) -> dict[str, Any]:
        """返回 ``metadata["tools"]`` 工具配置（副本）。"""
        config = self.metadata.get(TOOL_CONFIG_KEY)
        return dict(config) if isinstance(config, Mapping) else {}

    def tool_options(self, tool_name: str | None = None) -> dict[str, Any]:
        """返回工具选项；传入工具名时合并该工具的专属配置。

        配置结构::

            metadata["tools"] = {
                "enabled": ["read_file"],          # 白名单（非空时生效）
                "disabled": ["bash"],             # 黑名单
                "timeout": 30,                    # 全局选项
                "options": {"bash": {"timeout": 60}},  # 每个工具的选项
            }
        """
        config = self.tool_config()
        options = {
            key: value
            for key, value in config.items()
            if key not in {"enabled", "disabled", "options"}
        }
        if tool_name is not None:
            per_tool = config.get("options")
            if isinstance(per_tool, Mapping):
                specific = per_tool.get(tool_name)
                if isinstance(specific, Mapping):
                    options.update(specific)
        return options

    def tool_enabled(self, tool_name: str) -> bool:
        """按会话工具配置判断工具是否启用。"""
        config = self.tool_config()
        disabled = config.get("disabled")
        if isinstance(disabled, (list, tuple, set, frozenset)) and tool_name in disabled:
            return False
        enabled = config.get("enabled")
        if isinstance(enabled, (list, tuple, set, frozenset)) and enabled:
            return tool_name in enabled
        return True

    def configure_tools(
        self,
        *,
        enabled: Iterable[str] | None = None,
        disabled: Iterable[str] | None = None,
        options: Mapping[str, Mapping[str, Any]] | None = None,
        **values: Any,
    ) -> "Session":
        """写入会话级工具配置（合并到 ``metadata["tools"]``）。"""
        config = self.tool_config()
        if enabled is not None:
            config["enabled"] = list(enabled)
        if disabled is not None:
            config["disabled"] = list(disabled)
        if options is not None:
            merged: dict[str, Any] = dict(config.get("options") or {})
            for name, value in options.items():
                specific = dict(merged.get(name) or {})
                specific.update(value or {})
                merged[str(name)] = specific
            config["options"] = merged
        config.update(values)
        return self.update_metadata(**{TOOL_CONFIG_KEY: config})

    def tool_context(
        self,
        *,
        metadata: Mapping[str, Any] | None = None,
        approve: ApproveFn | None = None,
        cancelled: CancelFn | None = None,
    ) -> "Session":
        """返回本次工具执行的会话视图。

        视图与源会话共享 ``session_id``、工作区和回调，并把调用方 ``metadata``
        （例如引擎本次调用的元数据）合并进会话元数据后交给工具使用；会话元数据
        优先，并且始终写入当前 ``session_id``。
        """
        merged = dict(metadata or {})
        merged.update(self.metadata)
        merged["session_id"] = self.session_id
        return replace(
            self,
            metadata=merged,
            approve=approve or self.approve,
            cancelled=cancelled or self.cancelled,
            context=self.context,
        )

    def runtime_context(self, **kwargs: Any) -> "Session":
        """兼容旧命名：等价于 ``tool_context()``。"""
        return self.tool_context(**kwargs)

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


__all__ = ["Session", "ApproveFn", "CancelFn", "TOOL_CONFIG_KEY", "utc_now"]

