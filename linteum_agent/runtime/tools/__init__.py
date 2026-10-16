"""Tools layer (design doc §21–§24), milestone S4.

Public surface:

* :mod:`linteum_agent.runtime.tools.spec` — :class:`ToolSpec` + the dependency-free schema validator.
* :mod:`linteum_agent.runtime.tools.decorator` — the ``@tool`` decorator.
* :mod:`linteum_agent.runtime.tools.registry` — :class:`ToolRegistry` (assembly-time allowlist).
* :mod:`linteum_agent.runtime.tools.runtime` — :class:`ToolRuntime`, the only ``tool.call.requested``
  subscriber and the only publisher of ``tool.call.*``.
* :mod:`linteum_agent.runtime.tools.sandbox` — :class:`Sandbox` and the L0/L1/L2 backends.
* :mod:`linteum_agent.runtime.tools.results` — :class:`ResultStore` (per-``call_id`` idempotency).
* :mod:`linteum_agent.runtime.tools.approval` — :class:`ApprovalGate` (human-in-the-loop).
* :mod:`linteum_agent.runtime.tools.mcp_bridge` — :class:`McpBridge` (remote MCP tools).
* :mod:`linteum_agent.runtime.tools.workspace` — :class:`Workspace` + ``make_file_tools`` (confined file tools).

A tool function never knows about events: the runtime turns its outcome into
``tool.call.completed`` / ``tool.call.failed`` (P3/P4).
"""

from .approval import ApprovalGate, redact
from .decorator import spec_of, tool
from .errors import (
    ApprovalDenied,
    DuplicateTool,
    SafetyViolation,
    SandboxError,
    ToolCancelled,
    ToolError,
    ToolExecutionError,
    ToolUnavailable,
    UnknownTool,
    ValidationError,
    WorkspaceUnavailable,
    WorkspaceViolation,
)
from .mcp_bridge import KIND_TO_EVENT, McpBridge, McpFrame, McpRouter, McpServer
from .registry import ToolRegistry
from .results import (
    InMemoryResultStore,
    NullResultStore,
    ResultStore,
    SqliteResultStore,
)
from .runtime import DEFAULT_INLINE_LIMIT, ToolRuntime
from .sandbox import (
    BaseBackend,
    InProcessBackend,
    Sandbox,
    SubprocessBackend,
    ThreadBackend,
    WorkerBackend,
)
from .spec import BACKENDS, SIDE_EFFECTS, ToolSpec, validate_args
from .workspace import (
    Workspace,
    WorkspaceRegistry,
    WorkspaceToolRegistries,
    make_bash_tool,
    make_file_tools,
)
from . import testing

__all__ = [
    # spec / decorator / registry
    "ToolSpec",
    "validate_args",
    "SIDE_EFFECTS",
    "BACKENDS",
    "tool",
    "spec_of",
    "ToolRegistry",
    # runtime
    "ToolRuntime",
    "DEFAULT_INLINE_LIMIT",
    # sandbox
    "Sandbox",
    "WorkerBackend",
    "BaseBackend",
    "InProcessBackend",
    "ThreadBackend",
    "SubprocessBackend",
    # results
    "ResultStore",
    "InMemoryResultStore",
    "NullResultStore",
    "SqliteResultStore",
    # approval
    "ApprovalGate",
    "redact",
    # mcp
    "McpBridge",
    "McpFrame",
    "McpRouter",
    "McpServer",
    "KIND_TO_EVENT",
    # errors
    "ToolError",
    "UnknownTool",
    "ToolUnavailable",
    "DuplicateTool",
    "ValidationError",
    "ToolExecutionError",
    "SandboxError",
    "SafetyViolation",
    "ToolCancelled",
    "ApprovalDenied",
    "WorkspaceViolation",
    "WorkspaceUnavailable",
    # workspace / file tools
    "Workspace",
    "WorkspaceRegistry",
    "WorkspaceToolRegistries",
    "make_file_tools",
    "make_bash_tool",
    # offline testing helpers
    "testing",
]
