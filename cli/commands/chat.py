"""``atom chat`` 的实现：构造 :class:`AgentRunner` 并对话。

本模块只做**宿主适配**（读取 argv -> 调用 atom_agent），不包含任何 Agent 业务逻辑；
真正的能力来自 ``atom_agent.run_agent.build_agent``。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any


def _workspace(args) -> Path:
    workspace = getattr(args, "workspace", None)
    return Path(workspace).expanduser() if workspace else Path.cwd()


def _build_runner(args):
    """按 argv 构造 ``AgentRunner``（惰性 import，避免为其它子命令付启动成本）。"""
    from atom_agent.run_agent import build_agent

    return build_agent(
        workspace=_workspace(args),
        session_id=getattr(args, "session", None),
        config_path=getattr(args, "config", None),
        auto_approve=bool(getattr(args, "auto_approve", False)),
        enable_bash=bool(getattr(args, "enable_bash", False)),
        max_tool_rounds=int(getattr(args, "max_tool_rounds", 20) or 20),
    )


def _prepare_session(runner, args) -> None:
    """决定「新建」还是「继续」：``--new`` 新建，否则载入构造时查到的既有会话。"""
    if getattr(args, "new", False):
        runner.create_context()
    elif getattr(runner, "has_unloaded_session", False):
        runner.load_session()


class _ConsolePrinter:
    """把事件流渲染成终端输出（正文 / 思维链 / 工具调用）。"""

    def __init__(self) -> None:
        self._pending_text = False

    def on_event(self, event: dict[str, Any]) -> None:
        event_type = event.get("type")
        if event_type == "message_update":
            update = event.get("assistant_message_event") or {}
            kind = update.get("type")
            if kind == "text_delta":
                self._write(update.get("text", ""))
            elif kind == "reasoning_delta":
                self._write(f"\033[90m{update.get('text', '')}\033[0m")
        elif event_type == "tool_execution_start":
            self.ensure_newline()
            print(f"[tool] {event.get('tool_name')} {event.get('args')}")
        elif event_type == "tool_execution_end":
            status = "error" if event.get("is_error") else "ok"
            print(f"[tool] {event.get('tool_name')} -> {status}")

    def _write(self, text: str) -> None:
        if not text:
            return
        sys.stdout.write(text)
        sys.stdout.flush()
        self._pending_text = True

    def ensure_newline(self) -> None:
        if self._pending_text:
            sys.stdout.write("\n")
            sys.stdout.flush()
            self._pending_text = False


def run_chat(args) -> int:
    try:
        runner = _build_runner(args)
    except Exception as exc:  # 配置缺失 / API Key 未设置等
        print(f"无法初始化 Agent：{exc}", file=sys.stderr)
        return 2

    try:
        _prepare_session(runner, args)
        prompt = getattr(args, "prompt", None)
        if prompt:
            return _one_shot(runner, args, prompt)
        return _repl(runner, args)
    finally:
        runner.close()


def _one_shot(runner, args, prompt: str) -> int:
    stream = bool(getattr(args, "stream", True))
    as_json = bool(getattr(args, "json", False))

    if stream and not as_json:
        printer = _ConsolePrinter()
        detach = runner.add_listener(printer)
        try:
            for _chunk in runner.stream(prompt):
                pass
        finally:
            detach()
        printer.ensure_newline()
        return 0 if runner.response().ok else 1

    response = runner.send(prompt)
    if as_json:
        print(response.to_json())
    else:
        if response.reasoning:
            print(f"\033[90m{response.reasoning}\033[0m")
        print(response.text)
    return 0 if response.ok else 1


def _repl(runner, args) -> int:
    print(f"atom 会话 {runner.session_id}（输入 exit / quit 退出）")
    printer = _ConsolePrinter()
    detach = runner.add_listener(printer)
    try:
        while True:
            try:
                line = input("> ")
            except EOFError:
                print()
                break
            text = line.strip()
            if not text:
                continue
            if text in ("exit", "quit", ":q"):
                break
            try:
                for _chunk in runner.stream(text):
                    pass
            except Exception as exc:  # 单轮失败不退出 REPL
                print(f"请求失败：{exc}", file=sys.stderr)
            printer.ensure_newline()
    finally:
        detach()
    return 0
