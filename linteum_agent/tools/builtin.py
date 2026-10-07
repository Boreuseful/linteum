from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from .core import ApproveFn, Session, Tool, ToolResult
from .registry import ToolRegistry


def _safe_path(root_dir: str | Path, path: str) -> Path:
    root = Path(root_dir).expanduser().resolve()
    candidate = (root / path).resolve()
    if candidate != root and root not in candidate.parents:
        raise ValueError(f"file path escapes the tool root directory: {path}")
    return candidate


def read_file(path: str, *, root_dir: str | Path = ".", encoding: str = "utf-8") -> str:
    """Read a text file within the root directory."""
    return _safe_path(root_dir, path).read_text(encoding=encoding)


def write_file(
    path: str,
    content: str,
    *,
    root_dir: str | Path = ".",
    encoding: str = "utf-8",
) -> dict[str, Any]:
    """Write a text file within the root directory, creating parent directories automatically."""
    target = _safe_path(root_dir, path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding=encoding)
    return {"path": str(target), "bytes": len(content.encode(encoding))}


def run_bash(
    command: str,
    *,
    root_dir: str | Path = ".",
    cwd: str | Path | None = None,
    timeout: float = 30,
) -> dict[str, Any]:
    """Execute a Bash command within the tool root directory.

    The command runs via ``bash -c``, so pipes, redirections, and environment variables are supported.
    Callers should enable this tool only for trusted input; ``shell=False`` only means Python does not
    additionally nest a shell.
    """
    if not isinstance(command, str) or not command.strip():
        raise ValueError("bash command must not be empty")
    if timeout <= 0:
        raise ValueError("bash timeout must be greater than 0")

    root = Path(root_dir).expanduser().resolve()
    working_dir = root if cwd is None else _safe_path(root, str(cwd))
    if not working_dir.is_dir():
        raise NotADirectoryError(f"Bash working directory does not exist: {working_dir}")
    try:
        completed = subprocess.run(
            ["bash", "-c", command],
            cwd=working_dir,
            capture_output=True,
            text=True,
            timeout=timeout,
            shell=False,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError(f"Bash command timed out after {timeout} seconds") from exc
    return {
        "returncode": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
        "cwd": str(working_dir),
    }


def list_files(path: str = ".", *, root_dir: str | Path = ".", recursive: bool = False) -> list[str]:
    """List files within the workspace; return POSIX paths relative to the workspace."""
    directory = _safe_path(root_dir, path)
    if not directory.is_dir():
        raise NotADirectoryError(f"directory does not exist: {path}")
    entries = directory.rglob("*") if recursive else directory.iterdir()
    root = Path(root_dir).expanduser().resolve()
    return sorted(str(item.relative_to(root).as_posix()) for item in entries if item.is_file())


def tool_result_content(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, default=str)


def create_read_tool() -> Tool:
    def execute(args: dict[str, Any], context: Session) -> ToolResult:
        try:
            return ToolResult(read_file(args["path"], root_dir=context.effective_workspace,
                                        encoding=args.get("encoding", "utf-8")))
        except Exception as exc:
            return ToolResult(str(exc), is_error=True)
    return Tool("read_file", "Read a text file within the workspace", {
        "type": "object", "properties": {
            "path": {"type": "string"}, "encoding": {"type": "string", "default": "utf-8"}},
        "required": ["path"], "additionalProperties": False}, execute)


def create_list_files_tool() -> Tool:
    def execute(args: dict[str, Any], context: Session) -> ToolResult:
        try:
            value = list_files(args.get("path", "."), root_dir=context.effective_workspace,
                               recursive=args.get("recursive", False))
            return ToolResult("\n".join(value) or "(empty directory)", details={"files": value, "count": len(value)})
        except Exception as exc:
            return ToolResult(str(exc), is_error=True)
    return Tool("list_files", "List files within the workspace", {
        "type": "object", "properties": {"path": {"type": "string"}, "recursive": {"type": "boolean"}},
        "additionalProperties": False}, execute)


def create_write_file_tool() -> Tool:
    def execute(args: dict[str, Any], context: Session) -> ToolResult:
        try:
            value = write_file(args["path"], args["content"], root_dir=context.effective_workspace,
                               encoding=args.get("encoding", "utf-8"))
            return ToolResult(json.dumps(value, ensure_ascii=False), details=value)
        except Exception as exc:
            return ToolResult(str(exc), is_error=True)
    return Tool("write_file", "Write a text file within the workspace", {
        "type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"},
        "encoding": {"type": "string", "default": "utf-8"}}, "required": ["path", "content"],
        "additionalProperties": False}, execute)


def create_bash_tool(timeout: float = 30) -> Tool:
    def execute(args: dict[str, Any], context: Session) -> ToolResult:
        try:
            # The session can configure a per-tool timeout: session.tools["options"]["bash"]["timeout"]
            effective_timeout = float(context.tool_options("bash").get("timeout", timeout))
            value = run_bash(args["command"], root_dir=context.effective_workspace,
                             cwd=args.get("cwd"), timeout=effective_timeout)
            return ToolResult(json.dumps(value, ensure_ascii=False), is_error=value["returncode"] != 0, details=value)
        except Exception as exc:
            return ToolResult(str(exc), is_error=True)
    return Tool("bash", "Execute Bash commands within the workspace", {
        "type": "object", "properties": {"command": {"type": "string"}, "cwd": {"type": "string"}},
        "required": ["command"], "additionalProperties": False}, execute, requires_approval=True)


def create_builtin_tools(*, include_bash: bool = False, bash_timeout: float = 30,
                         workspace: str | Path = ".", session: Session | None = None,
                         approve: ApproveFn | None = None) -> ToolRegistry:
    """Create the built-in tools in the target Tool factory format.

    Tool config is stored in a ``Session``: ``session=`` can directly reuse the caller's session,
    otherwise a session with ``session_id="default"`` is created from ``workspace``.

    ``approve`` is passed to the ``ToolRegistry`` (written into ``session.approve``): when ``bash`` is
    enabled it is the approval callback for the ``requires_approval=True`` tool; when omitted
    (``None``) the callback already in the session is reused.
    """
    session = session or Session(session_id="default", workspace=Path(workspace))
    tools = [create_read_tool(), create_list_files_tool(), create_write_file_tool()]
    if include_bash:
        tools.append(create_bash_tool(bash_timeout))
    return ToolRegistry(tools, session=session, approve=approve)

