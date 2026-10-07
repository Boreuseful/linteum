"""Agent tools: definitions, registry, validation, and built-ins.

The tool runtime context (the former ``RuntimeContext``) has been merged into ``context.Session``:
``RuntimeContext`` is kept as a compatibility alias; new code should use ``Session`` directly.
"""

from .builtin import (create_bash_tool, create_builtin_tools,
                      create_list_files_tool, create_read_tool, create_write_file_tool,
                      list_files, read_file, run_bash, write_file)
from .core import ApproveFn, RuntimeContext, Session, Tool, ToolResult
from .registry import ToolRegistry

__all__ = [
    "Session",
    "RuntimeContext",
    "ApproveFn",
    "Tool",
    "ToolResult",
    "ToolRegistry",
    "create_builtin_tools",
    "create_read_tool",
    "create_list_files_tool",
    "create_write_file_tool",
    "create_bash_tool",
    "read_file",
    "list_files",
    "write_file",
    "run_bash",
]