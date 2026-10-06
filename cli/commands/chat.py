"""``linteum chat`` 的实现：构造 :class:`AgentRunner` 并对话。

本模块只做**宿主适配**（读取 argv -> 调用 linteum_agent），不包含任何 Agent 业务逻辑；
真正的能力来自 ``linteum_agent.run_agent.build_agent``。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any


def _workspace(args) -> Path:
    workspace = getattr(args, "workspace", None)
    return Path(workspace).expanduser() if workspace else Path.cwd()


# 工具轮次超限时的友好提示：只给一句可操作的说明，不直接暴露底层 RuntimeError 文本。
MAX_TOOL_ROUNDS_NOTICE = "（已达到最大工具调用轮次，本轮已停止；可用 --max-tool-rounds 调大上限）"


def _warn_max_tool_rounds() -> None:
    """打印「工具轮次超限」的友好提示（正文走 stdout，提示走 stderr）。"""
    print(MAX_TOOL_ROUNDS_NOTICE, file=sys.stderr)


def _build_runner(args):
    """按 argv 构造 ``AgentRunner``（惰性 import，避免为其它子命令付启动成本）。

    会话选择：显式 ``-s/--session`` > **当前会话**（``linteum sessions new/switch`` 设置）>
    由 ``AgentRunner`` 按工作区自动查找 / 新建。
    """
    from linteum_agent.run_agent import build_agent
    from cli.session_state import get_current_session

    session_id = getattr(args, "session", None) or get_current_session()
    return build_agent(
        workspace=_workspace(args),
        session_id=session_id,
        config_path=getattr(args, "config", None),
        auto_approve=bool(getattr(args, "auto_approve", False)),
        enable_bash=bool(getattr(args, "enable_bash", False)),
        max_tool_rounds=int(getattr(args, "max_tool_rounds", 20) or 20),
    )


def _prepare_session(runner, args) -> None:
    """决定「新建」还是「继续」：``--new`` 新建并设为当前会话，否则载入查到的既有会话。"""
    if getattr(args, "new", False):
        from cli.session_state import set_current_session

        runner.create_context()
        set_current_session(runner.session_id)
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
    from linteum_agent.agent import MaxToolRoundsError

    stream = bool(getattr(args, "stream", True))
    as_json = bool(getattr(args, "json", False))

    if stream and not as_json:
        printer = _ConsolePrinter()
        detach = runner.add_listener(printer)
        try:
            for _chunk in runner.stream(prompt):
                pass
        except MaxToolRoundsError:
            # 不抛出、不打印异常；正文已由 printer 流式输出，这里只补一句友好提示。
            printer.ensure_newline()
            _warn_max_tool_rounds()
            return 1
        finally:
            detach()
        printer.ensure_newline()
        return 0 if runner.response().ok else 1

    try:
        response = runner.send(prompt)
    except MaxToolRoundsError:
        # 用已聚合的响应兜底（正文可能为空），而不是把 RuntimeError 打到终端。
        response = runner.response()
        if not as_json:
            if response.reasoning:
                print(f"\033[90m{response.reasoning}\033[0m")
            print(response.text)
        _warn_max_tool_rounds()
        return 1
    if as_json:
        print(response.to_json())
    else:
        if response.reasoning:
            print(f"\033[90m{response.reasoning}\033[0m")
        print(response.text)
    return 0 if response.ok else 1


def _repl(runner, args) -> int:
    from linteum_agent.agent import MaxToolRoundsError

    print(f"linteum 会话 {runner.session_id}（输入 exit / quit 退出）")
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
            except MaxToolRoundsError:
                # 单轮工具轮次超限：不打印异常，友好提示后继续 REPL。
                printer.ensure_newline()
                _warn_max_tool_rounds()
            except Exception as exc:  # 单轮失败不退出 REPL
                print(f"请求失败：{exc}", file=sys.stderr)
            printer.ensure_newline()
    finally:
        detach()
    return 0
