from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from .core import Session, Tool, ToolResult
from .registry import ToolRegistry


def _safe_path(root_dir: str | Path, path: str) -> Path:
    root = Path(root_dir).expanduser().resolve()
    candidate = (root / path).resolve()
    if candidate != root and root not in candidate.parents:
        raise ValueError(f"文件路径超出工具根目录: {path}")
    return candidate


def read_file(path: str, *, root_dir: str | Path = ".", encoding: str = "utf-8") -> str:
    """读取根目录内的文本文件。"""
    return _safe_path(root_dir, path).read_text(encoding=encoding)


def write_file(
    path: str,
    content: str,
    *,
    root_dir: str | Path = ".",
    encoding: str = "utf-8",
) -> dict[str, Any]:
    """写入根目录内的文本文件，并自动创建父目录。"""
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
    """在工具根目录内执行 Bash 命令。

    命令通过 ``bash -c`` 执行，因此支持管道、重定向和环境变量。调用方应只
    对可信输入启用此工具；``shell=False`` 仅表示 Python 不再额外嵌套 shell。
    """
    if not isinstance(command, str) or not command.strip():
        raise ValueError("bash command 不能为空")
    if timeout <= 0:
        raise ValueError("bash timeout 必须大于 0")

    root = Path(root_dir).expanduser().resolve()
    working_dir = root if cwd is None else _safe_path(root, str(cwd))
    if not working_dir.is_dir():
        raise NotADirectoryError(f"Bash 工作目录不存在: {working_dir}")
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
        raise TimeoutError(f"Bash 命令执行超时（{timeout} 秒）") from exc
    return {
        "returncode": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
        "cwd": str(working_dir),
    }


def list_files(path: str = ".", *, root_dir: str | Path = ".", recursive: bool = False) -> list[str]:
    """列出工作区内的文件；返回相对于工作区的 POSIX 路径。"""
    directory = _safe_path(root_dir, path)
    if not directory.is_dir():
        raise NotADirectoryError(f"目录不存在: {path}")
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
    return Tool("read_file", "读取工作区内的文本文件", {
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
    return Tool("list_files", "列出工作区内的文件", {
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
    return Tool("write_file", "写入工作区内的文本文件", {
        "type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"},
        "encoding": {"type": "string", "default": "utf-8"}}, "required": ["path", "content"],
        "additionalProperties": False}, execute)


def create_bash_tool(timeout: float = 30) -> Tool:
    def execute(args: dict[str, Any], context: Session) -> ToolResult:
        try:
            # 会话可以为该工具单独配置超时：session.tools["options"]["bash"]["timeout"]
            effective_timeout = float(context.tool_options("bash").get("timeout", timeout))
            value = run_bash(args["command"], root_dir=context.effective_workspace,
                             cwd=args.get("cwd"), timeout=effective_timeout)
            return ToolResult(json.dumps(value, ensure_ascii=False), is_error=value["returncode"] != 0, details=value)
        except Exception as exc:
            return ToolResult(str(exc), is_error=True)
    return Tool("bash", "在工作区内执行 Bash 命令", {
        "type": "object", "properties": {"command": {"type": "string"}, "cwd": {"type": "string"}},
        "required": ["command"], "additionalProperties": False}, execute, requires_approval=True)


def create_builtin_tools(*, include_bash: bool = False, bash_timeout: float = 30,
                         workspace: str | Path = ".", session: Session | None = None) -> ToolRegistry:
    """按目标 Tool 工厂格式创建内置工具。

    工具配置保存在 ``Session`` 中：``session=`` 可以直接复用调用方会话，
    否则用 ``workspace`` 创建一个 ``session_id="default"`` 的会话。
    """
    session = session or Session(session_id="default", workspace=Path(workspace))
    tools = [create_read_tool(), create_list_files_tool(), create_write_file_tool()]
    if include_bash:
        tools.append(create_bash_tool(bash_timeout))
    return ToolRegistry(tools, session=session)

