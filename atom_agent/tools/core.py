"""The public Tool contracts used by the agent tool runtime."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable


@dataclass
class RuntimeContext:
    """Tool 执行时使用的运行环境。

    它是 Context 层 ``Session`` 的近似实现：只有工作区、元数据和回调，属于工具
    注册表级别，不区分 ``session_id``。``Session.runtime_context()`` 会把会话状态
    转换成这个对象，因此 ``metadata`` 中通常包含 ``session_id`` 和会话元数据。
    """

    workspace: Path = field(default_factory=Path.cwd)
    metadata: dict[str, Any] = field(default_factory=dict)
    approve: Callable[[str, dict[str, Any]], bool] | None = None
    cancelled: Callable[[], bool] | None = None

    def __post_init__(self) -> None:
        self.workspace = Path(self.workspace).expanduser().resolve()

    def log(self, message: str) -> None:
        callback = self.metadata.get("log")
        if callable(callback):
            callback(message)

    def is_cancelled(self) -> bool:
        return bool(self.cancelled and self.cancelled())

    @classmethod
    def cwd(cls) -> "RuntimeContext":
        return cls(Path.cwd())


@dataclass
class ToolResult:
    content: str
    is_error: bool = False
    details: dict[str, Any] | None = None


ExecuteFn = Callable[[dict[str, Any], RuntimeContext], ToolResult]


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    execute_fn: ExecuteFn
    enabled: bool = True
    requires_approval: bool = False

    def execute(self, arguments: dict[str, Any], context: RuntimeContext) -> ToolResult:
        return self.execute_fn(arguments, context)

