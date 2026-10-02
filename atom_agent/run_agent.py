"""Agent 运行入口：给 CLI / TUI / Web 等宿主程序提供统一的 Agent 访问方式。

本模块把「Agent 初始化 + 上下文存储 + Tool/Skill 注册 + 事件响应」收敛成一个门面
（``AgentRunner``）。宿主程序（命令行、TUI、Web 服务、其他工具）只需构造一次
``AgentRunner``，然后：

- 发送用户消息：``send()`` / ``asend()`` / ``stream()`` / ``astream()``
- 订阅事件做实时渲染：``on()`` / ``add_listener()``（事件流与宿主类型无关）
- 读取响应：``response()`` -> ``AgentResponse``（正文 / 思维链 / 工具调用 / usage）
- 持久化上下文：``save_context()`` / ``load_context()`` / ``close()``
- 列出全部会话：``list_sessions()`` / ``AgentRunner.list_sessions()`` -> ``SessionInfo`` 摘要

会话标识默认使用 UUID（``uuid4``），并把同一份上下文同时保存到**项目根目录**与
**工作区**下的 ``.agent/sessions/<session_id>.json``（两个位置的文件互为副本）。

会话生命周期（构造**主动查找**，加载**显式**）：

- 构造 ``AgentRunner`` **主动查找**会话：先看显式 ``session_id`` / ``context_path``，
  否则按 ``session_priority``（**默认工作区优先**）查找工作区与项目根目录下已有的上下文，
  命中即把 ``session_id`` 设为该会话并记入 ``found_context_path``；没命中才用新 ``uuid4``；
- 工作区里没有、回退到项目根目录时还要比对 JSON 里记录的 ``workspace``：与当前工作区不一致的
  **不是**本次要查找的上下文（项目级是多个工作区共享的），视为未命中（见
  ``context_matches_workspace()``）；
- 但构造**不载入内容、不创建文件**：``loaded_context_paths`` 为空、``get_context()`` 为空，
  直到显式调用 ``load_session()``（或 ``load_context(path)``）；
- 查到既有会话却未显式载入时，``send()`` / ``stream()`` / ``save_context()`` 会先报错
  （``has_unloaded_session``），避免用空历史覆盖既有内容；
- 开始新对话：显式调用 ``create_context()`` —— 未指定 ``session_id`` 时**每次都生成
  新的 ``uuid4``**，因此不会覆盖 / 重置其它会话的上下文；
- 落盘：显式 ``save_context()``，或 ``close()``（仅当本轮确实有改动）。


组装关系（各层职责）：::

    config.yaml  -> ConfigStore -> LLMConfig -> LLMClient
    Session(JSON) -> ContextStore -> Session -> ContextMiddleware
    ToolRegistry  + SkillRegistry -> AgentEngine
    AgentEngine -> EventBus -> ResponseCollector -> AgentResponse
    create_approve_callback() -> session.approve -> ToolRegistry（bash 等需授权工具）

事件流（``stream()`` 模式）::

    agent_start
      -> message_start(user)
      -> turn_start
      -> message_update(text_delta / reasoning_delta) ...
      -> tool_execution_start -> tool_execution_update -> tool_execution_end
      -> turn_end
      -> turn_start
      -> message_update ... -> message_start -> message_end -> turn_end
      -> AgentStreamChunk(done=True)

``run()`` 模式没有 ``message_update`` 增量，最终文本由 ``message_end`` / ``turn_end`` /
``agent_end`` 事件携带；``ResponseCollector`` 对两种模式都会产出同一份 ``AgentResponse``，
因此 TUI/Web 等宿主不需要自己区分同步 / 流式（详见 ``/workspace/test/test_run/README.md``）。

本模块**不包含 CLI 解析**：命令行、TUI、Web 都应作为宿主程序调用 ``build_agent()`` /
``AgentRunner``，并自行决定输入输出方式。
"""

from __future__ import annotations

import json
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Iterable, Iterator, Mapping, TextIO

# ----------------------------------------------------------------------
# 兼容两种导入方式：
# 1) ``from atom_agent.run_agent import ...``（包内相对导入）；
# 2) 直接把 ``/workspace/atom_agent`` 加入 sys.path 后 ``import run_agent``。
# skills 包内部使用 ``from ..tools import ...``，因此后者必须把项目根目录加入
# sys.path，再以 ``atom_agent.*`` 绝对导入，避免 "attempted relative import"。
# ----------------------------------------------------------------------
try:  # pragma: no cover - 取决于调用方式
    from .agent.engine import AgentEngine, AgentResult, AgentStreamChunk
    from .config import ConfigStore, LLMConfig
    from .config.store import project_root
    from .context.middleware import ContextMiddleware
    from .context.session import ApproveFn, Session
    from .context.store import ContextStore
    from .evnts import AgentEvent, AgentEventListener, EventBus
    from .messages.agent_message import (
        AssistantMessage,
        MessageInput,
        MessageInputs,
        SystemPrompt,
        UserPrompt,
        normalize_messages,
    )
    from .model.client import LLMClient
    from .skills import SkillRegistry
    from .tools import Tool, ToolRegistry, create_builtin_tools
except ImportError:  # 直接导入 run_agent：补上项目根目录后改用绝对导入
    _SCRIPT_ROOT = Path(__file__).resolve().parents[1]
    if str(_SCRIPT_ROOT) not in sys.path:
        sys.path.insert(0, str(_SCRIPT_ROOT))

    from atom_agent.agent.engine import AgentEngine, AgentResult, AgentStreamChunk
    from atom_agent.config import ConfigStore, LLMConfig
    from atom_agent.config.store import project_root
    from atom_agent.context.middleware import ContextMiddleware
    from atom_agent.context.session import ApproveFn, Session
    from atom_agent.context.store import ContextStore
    from atom_agent.evnts import AgentEvent, AgentEventListener, EventBus
    from atom_agent.messages.agent_message import (
        AssistantMessage,
        MessageInput,
        MessageInputs,
        SystemPrompt,
        UserPrompt,
        normalize_messages,
    )
    from atom_agent.model.client import LLMClient
    from atom_agent.skills import SkillRegistry
    from atom_agent.tools import Tool, ToolRegistry, create_builtin_tools


__all__ = [
    "AgentResponse",
    "ResponseCollector",
    "AgentRunner",
    "build_agent",
    "run_agent",
    "create_approve_callback",
    "confirm_tool_call",
    "describe_tool_risk",
    "TOOL_RISK_NOTES",
    "new_session_id",
    "session_context_paths",
    "session_search_dirs",
    "context_json_workspace",
    "context_matches_workspace",
    "find_context_json",
    "find_context_jsons",
    "create_context_json",
    "SessionInfo",
    "list_sessions",
    "SESSION_SCOPE_WORKSPACE",
    "SESSION_SCOPE_PROJECT",
    "SESSION_SCOPE_CONTEXT",
    "SESSION_PRIORITY_WORKSPACE",
    "SESSION_PRIORITY_PROJECT",
    "DEFAULT_SESSION_PRIORITY",
]

# 未指定会话时使用的占位标签；真实会话默认使用 ``new_session_id()``（uuid4）。
DEFAULT_SESSION_ID = "default"
DEFAULT_MAX_TOOL_ROUNDS = 8

# 会话上下文默认存储目录：项目根目录与工作区各存一份（``<root>/.agent/sessions``）。
DEFAULT_SESSION_DIRNAME = Path(".agent") / "sessions"

# 会话查找优先级：同一会话在两个位置各有一份副本，查找时谁优先。
# 默认 ``workspace`` = **工作区优先**，即先看当前工作区下的 ``.agent/sessions``，
# 再看项目根目录；显式传 ``SESSION_PRIORITY_PROJECT`` 可让项目根目录优先。
SESSION_PRIORITY_WORKSPACE = "workspace"
SESSION_PRIORITY_PROJECT = "project"
DEFAULT_SESSION_PRIORITY = SESSION_PRIORITY_WORKSPACE

# 默认 Skill 搜索目录：系统级在用户目录下，项目级在工作区 ``.agent/skills``。
# 目录不存在时 SkillRegistry.discover() 会自动跳过。
_SYSTEM_SKILL_DIR = Path.home() / ".agent" / "skills"
_PROJECT_SKILL_DIRNAME = Path(".agent") / "skills"

# 事件类型常量：避免调用方硬编码字符串。
EVENT_AGENT_START = "agent_start"
EVENT_AGENT_END = "agent_end"
EVENT_TURN_START = "turn_start"
EVENT_TURN_END = "turn_end"
EVENT_MESSAGE_START = "message_start"
EVENT_MESSAGE_UPDATE = "message_update"
EVENT_MESSAGE_END = "message_end"
EVENT_TOOL_START = "tool_execution_start"
EVENT_TOOL_UPDATE = "tool_execution_update"
EVENT_TOOL_END = "tool_execution_end"

ALL_EVENTS = (
    EVENT_AGENT_START,
    EVENT_AGENT_END,
    EVENT_TURN_START,
    EVENT_TURN_END,
    EVENT_MESSAGE_START,
    EVENT_MESSAGE_UPDATE,
    EVENT_MESSAGE_END,
    EVENT_TOOL_START,
    EVENT_TOOL_UPDATE,
    EVENT_TOOL_END,
)


# ----------------------------------------------------------------------
# 工具授权（approve）：风险提示 + 用户确认
# ----------------------------------------------------------------------
# 高风险工具的风险说明；``create_approve_callback()`` 提示风险时按工具名取用。
TOOL_RISK_NOTES: dict[str, str] = {
    "bash": "在工作区内执行任意 Bash 命令：可读写 / 删除文件、访问网络、启动子进程。",
    "write_file": "写入（覆盖）工作区内的文件。",
}

# 视为「授权」的回答（大小写不敏感）；其余输入一律视为拒绝。
_APPROVE_ANSWERS = ("y", "yes")


def _stdin_is_interactive() -> bool:
    """当前进程的标准输入是否可交互（不是管道 / 重定向 / 封闭流）。"""
    try:
        return bool(sys.stdin) and bool(sys.stdin.isatty())
    except (AttributeError, ValueError):  # pragma: no cover - 封闭的 stdin
        return False


def describe_tool_risk(tool_name: str, arguments: Mapping[str, Any] | None = None) -> str:
    """渲染一次待授权工具调用的风险提示文本（工具名 + 风险说明 + 调用参数）。

    未在 ``TOOL_RISK_NOTES`` 中登记的工具（例如宿主通过 ``extra_tools`` 注册、
    且 ``requires_approval=True`` 的自定义工具）给出一段通用提示。
    """
    note = TOOL_RISK_NOTES.get(
        tool_name, "该工具被标记为需要授权，请确认调用参数是否安全。"
    )
    lines = [f"工具 {tool_name} 请求执行。", f"风险提示：{note}"]
    if arguments:
        try:
            rendered = json.dumps(dict(arguments), ensure_ascii=False, default=str)
        except (TypeError, ValueError):  # pragma: no cover - 兜底
            rendered = str(arguments)
        lines.append(f"调用参数：{rendered}")
    return "\n".join(lines)


def confirm_tool_call(
    tool_name: str,
    arguments: dict[str, Any],
    *,
    input_fn: Callable[[str], str] | None = None,
    stream: TextIO | None = None,
    default: bool = False,
) -> bool:
    """把工具调用的风险提示显示给用户并等待确认，返回是否授权。

    - ``input_fn=None``（默认）：使用内置 ``input()``；当 ``stdin`` 不是终端（管道、
      CI、Web 进程等）时无法交互，直接返回 ``default``，避免卡住调用方；
    - 仅 ``y`` / ``yes``（大小写不敏感）视为授权，其它输入一律拒绝；
    - 用户按 Ctrl+C / Ctrl+D 等交互异常同样返回 ``default``；
    - ``stream`` 是提示输出流，默认 ``sys.stderr``（不污染正常输出，例如流式正文）。
    """
    output = stream if stream is not None else sys.stderr
    print(describe_tool_risk(tool_name, arguments), file=output, flush=True)

    prompt_fn = input_fn
    if prompt_fn is None:
        if not _stdin_is_interactive():
            verdict = "允许" if default else "拒绝"
            print(
                f"当前环境无法交互确认，按默认策略处理（{verdict}）：{tool_name}",
                file=output,
                flush=True,
            )
            return bool(default)
        prompt_fn = input

    try:
        answer = prompt_fn(f"是否允许执行 {tool_name}？[y/N] ")
    except (EOFError, KeyboardInterrupt, OSError):
        print(f"\n未获得有效确认，视为拒绝：{tool_name}", file=output, flush=True)
        return bool(default)
    return str(answer).strip().lower() in _APPROVE_ANSWERS


def create_approve_callback(
    *,
    auto_approve: bool = False,
    input_fn: Callable[[str], str] | None = None,
    stream: TextIO | None = None,
    default: bool = False,
) -> ApproveFn:
    """生成交给 ``ToolRegistry``（即 ``session.approve``）的工具授权回调。

    回调签名与 ``Session.approve`` 一致：``(tool_name, arguments) -> bool``。

    - ``auto_approve=True``：直接返回 ``True`` —— 不提示风险、不询问用户（全部授权）；
    - ``auto_approve=False``（默认）：打印风险提示并询问用户确认（见 ``confirm_tool_call()``）；
    - ``input_fn`` / ``stream`` / ``default`` 透传给 ``confirm_tool_call()``，便于 TUI /
      Web 等宿主接管交互（默认使用 ``input()`` + ``sys.stderr``，且不可交互时拒绝）。
    """
    def approve(tool_name: str, arguments: dict[str, Any]) -> bool:
        if auto_approve:
            return True
        return confirm_tool_call(
            tool_name, arguments, input_fn=input_fn, stream=stream, default=default
        )

    return approve


# ----------------------------------------------------------------------
# 会话标识与上下文路径
# ----------------------------------------------------------------------


def new_session_id() -> str:
    """生成一个新的会话 ID（``uuid4`` 的规范字符串形式）。"""
    return str(uuid.uuid4())


def session_context_paths(
    session_id: str,
    workspace: str | Path,
    *,
    project_dir: str | Path | None = None,
    dirname: str | Path = DEFAULT_SESSION_DIRNAME,
) -> list[Path]:
    """返回某个会话的两份上下文存储路径：项目根目录 + 工作区。

    路径形如 ``<root>/<dirname>/<session_id>.json``。项目根目录与工作区恰好相同时
    会去重，因此最终只返回一份。
    """
    root = Path(project_dir).expanduser() if project_dir is not None else project_root()
    roots = [root, Path(workspace).expanduser()]
    paths: list[Path] = []
    for item in roots:
        candidate = item / dirname / f"{session_id}.json"
        if candidate not in paths:
            paths.append(candidate)
    return paths


def _normalize_session_priority(priority: str | None) -> str:
    """把优先级归一化为 ``workspace`` / ``project``；非法值抛 ``ValueError``。"""
    if priority is None:
        return DEFAULT_SESSION_PRIORITY
    value = str(priority).strip().lower()
    if value in (SESSION_PRIORITY_WORKSPACE, SESSION_PRIORITY_PROJECT):
        return value
    raise ValueError(
        f"priority 需要 {SESSION_PRIORITY_WORKSPACE!r} 或 "
        f"{SESSION_PRIORITY_PROJECT!r}，实际为 {priority!r}"
    )


def session_search_dirs(
    workspace: str | Path,
    *,
    project_dir: str | Path | None = None,
    dirname: str | Path = DEFAULT_SESSION_DIRNAME,
    priority: str = DEFAULT_SESSION_PRIORITY,
) -> list[Path]:
    """返回会话查找目录，**按优先级排序**（默认为工作区在前、项目根目录在后）。

    两个根目录恰好相同时自动去重，因此通常只返回一个目录。
    """
    root = Path(project_dir).expanduser() if project_dir is not None else project_root()
    workspace_dir = Path(workspace).expanduser() / dirname
    project_leaf = root / dirname
    ordered = (
        [workspace_dir, project_leaf]
        if _normalize_session_priority(priority) == SESSION_PRIORITY_WORKSPACE
        else [project_leaf, workspace_dir]
    )

    dirs: list[Path] = []
    for item in ordered:
        if item not in dirs:
            dirs.append(item)
    return dirs


def context_json_workspace(
    path: str | Path,
    *,
    session_id: str | None = None,
) -> Path | None:
    """读取上下文 JSON 里记录的会话工作区（只读，不会创建任何文件）。

    - ``session_id`` 给定时读取该会话的记录；未给定时只在该文件恰好只含一个会话时读取；
    - 返回 ``None`` 表示「无法确定」：文件读不了 / 不是本模块的 ``ContextStore`` 格式 /
      该会话尚未记录工作区（version 1 旧格式，运行时继承当前工作区）。
    """
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    sessions = payload.get("sessions") if isinstance(payload, dict) else None
    if not isinstance(sessions, dict):
        return None

    entry: Any = None
    if session_id is not None:
        entry = sessions.get(session_id)
    elif len(sessions) == 1:
        entry = next(iter(sessions.values()))

    recorded = entry.get("workspace") if isinstance(entry, dict) else None
    if not recorded:
        return None
    return Path(str(recorded)).expanduser().resolve()


def context_matches_workspace(
    path: str | Path,
    workspace: str | Path,
    *,
    session_id: str | None = None,
) -> bool:
    """判断上下文 JSON 记录的会话工作区是否与 ``workspace`` 一致。

    - 文件里没有记录工作区（旧格式 / 无法读取）：按 ``Session`` 的语义是「运行时继承
      当前工作区」，因此返回 ``True``；
    - 记录了工作区：与 ``workspace`` 一起解析为绝对路径后比较，一致才返回 ``True``。

    项目根目录下的会话文件是**多个工作区共享**的（同一 session_id 只写 ``<id>.json``），
    因此回退到项目级查找时必须用它确认「这份上下文确实属于当前工作区」。
    """
    return _same_workspace(context_json_workspace(path, session_id=session_id), workspace)


def find_context_jsons(
    workspace: str | Path,
    *,
    project_dir: str | Path | None = None,
    dirname: str | Path = DEFAULT_SESSION_DIRNAME,
    session_id: str | None = None,
    priority: str = DEFAULT_SESSION_PRIORITY,
) -> list[Path]:
    """查找**已存在**的上下文 JSON（只读，不会创建文件或目录）。

    结果顺序 = 查找优先级（``priority``，默认 ``workspace`` = 工作区优先）：

    - 给定 ``session_id``：只在该会话的两份副本中查找，按优先级返回（存在几个返回几个）；
    - 未给定：先列出高优先级目录里的 ``*.json``（目录内按 mtime 从新到旧），
      再列出另一个目录的，同路径去重。

    工作区目录（``<workspace>/<dirname>``）下的文件天然属于当前工作区，直接采纳；
    **项目根目录**下的是多个工作区共享的，必须比对文件里记录的 ``workspace``
    （``context_matches_workspace()``）：与当前 ``workspace`` 不一致的**不是**本次要
    查找的上下文，直接跳过 —— 工作区里没有、项目级又不匹配时结果为空列表。

    注意与 ``session_context_paths()`` 的区别：后者是**写入/枚举顺序**（项目根目录 ->
    工作区），本函数是**查找顺序**（默认工作区 -> 项目根目录）。
    """
    directories = session_search_dirs(
        workspace, project_dir=project_dir, dirname=dirname, priority=priority
    )
    workspace_dir = Path(workspace).expanduser() / dirname

    def usable(candidate: Path) -> bool:
        """工作区目录里的文件直接采纳；项目级文件必须先比对记录的工作区。"""
        if candidate.parent == workspace_dir:
            return True
        return context_matches_workspace(candidate, workspace, session_id=session_id)

    if session_id is not None:
        found: list[Path] = []
        for directory in directories:
            candidate = directory / f"{session_id}.json"
            if candidate.is_file() and candidate not in found and usable(candidate):
                found.append(candidate)
        return found

    ordered: list[Path] = []
    for directory in directories:
        if not directory.is_dir():
            continue
        files = [path for path in directory.glob("*.json") if path.is_file()]
        files.sort(key=lambda path: path.stat().st_mtime, reverse=True)
        ordered.extend(path for path in files if path not in ordered and usable(path))
    return ordered


def find_context_json(
    workspace: str | Path,
    *,
    project_dir: str | Path | None = None,
    dirname: str | Path = DEFAULT_SESSION_DIRNAME,
    session_id: str | None = None,
    priority: str = DEFAULT_SESSION_PRIORITY,
) -> Path | None:
    """查找单个上下文 JSON；找到时返回**优先级最高**的那一个，否则返回 ``None``。

    这是「查找工作区上下文」的入口：默认**工作区优先**（``priority="workspace"``），
    工作区里没有才回退到项目根目录；同一目录内取最新修改的那一个。

    回退到项目根目录时还会比对文件里记录的 ``workspace``：与当前 ``workspace``
    不一致的**不是**本次要查找的上下文（项目级是多个工作区共享的），直接返回 ``None``
    —— 宁可让调用方新建会话，也不把别的工作区的历史当成本次上下文；旧格式没有记录
    工作区时按「运行时继承」处理，仍然采纳（详见 ``context_matches_workspace()``）。

    只读查找，不会创建任何文件；需要继续某个会话时请由调用方**显式**决定。
    """
    files = find_context_jsons(
        workspace,
        project_dir=project_dir,
        dirname=dirname,
        session_id=session_id,
        priority=priority,
    )
    return files[0] if files else None


def create_context_json(
    session_id: str,
    workspace: str | Path,
    *,
    path: str | Path | None = None,
    project_dir: str | Path | None = None,
    dirname: str | Path = DEFAULT_SESSION_DIRNAME,
    metadata: dict[str, Any] | None = None,
    overwrite: bool = False,
) -> list[Path]:
    """**主动**创建上下文 JSON（一式两份，或只建 ``path`` 指定的一份）。

    这是「创建上下文」的入口；只有调用了它（或 ``AgentRunner.create_context()``）才会
    在没有会话文件时新建文件。``AgentRunner`` 的构造过程本身不会创建任何 JSON。

    - 文件已存在且 ``overwrite=False``：保留原内容，仅返回其路径；
    - ``overwrite=True``：用全新会话覆盖写入；
    - 返回最终存在的文件路径列表（构造顺序：项目根目录 -> 工作区）。
    """
    resolved_workspace = Path(workspace).expanduser()
    targets = (
        [Path(path).expanduser()]
        if path is not None
        else session_context_paths(
            session_id, resolved_workspace, project_dir=project_dir, dirname=dirname
        )
    )

    store = ContextStore(workspace=resolved_workspace)
    store.ensure_session(session_id, workspace=resolved_workspace, metadata=metadata)

    created: list[Path] = []
    for target in targets:
        if target.is_file() and not overwrite:
            created.append(target)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        store.save_session(target, session_id)
        created.append(target)
    return created


# ----------------------------------------------------------------------
# 会话清单：列出全部会话（工作区 + 项目根目录，只读）
# ----------------------------------------------------------------------

# ``SessionInfo.scope`` 的取值：会话文件所在位置 / 来源。
SESSION_SCOPE_WORKSPACE = "workspace"   # <workspace>/<dirname>/<id>.json
SESSION_SCOPE_PROJECT = "project"       # <project_dir>/<dirname>/<id>.json
SESSION_SCOPE_CONTEXT = "context"       # 显式 context_path 指定的单个文件


@dataclass
class SessionInfo:
    """一个会话的**只读摘要**（id / 文件路径 / 位置 / 时间 / 规模 / 用量）。

    由 ``list_sessions()``（模块级）与 ``AgentRunner.list_sessions()`` 产出，供宿主列出
    「全部会话」；**不含完整消息历史** —— 需要历史时用 ``load_session()`` 载入，再
    ``get_context()`` 读取。``to_dict()`` / ``to_json()`` 便于直接返回给 Web / TUI。

    字段：

    - ``session_id``：会话 ID（文件里 ``sessions`` 的键）；
    - ``path``：该会话所在的上下文 JSON 文件；
    - ``scope``：文件位置 —— ``"workspace"`` / ``"project"`` / ``"context"``（显式
      ``context_path``）；
    - ``workspace``：文件里记录的工作区；``None`` = 「运行时继承当前工作区」；
    - ``matches_workspace``：记录的工作区是否与查询用 ``workspace`` 一致
      （``None`` 视为一致，口径同 ``context_matches_workspace()``）；
    - ``created_at`` / ``updated_at``：会话创建 / 更新时间（UTC ISO8601）；
    - ``message_count`` / ``round_count``：消息条数 / 对话轮数；
    - ``usage``：会话累计 token 用量；
    - ``metadata`` / ``tools`` / ``skills``：会话元数据与工具 / Skill 配置；
    - ``size`` / ``mtime``：文件字节数与最后修改时间。
    """

    session_id: str
    path: Path
    scope: str
    workspace: Path | None = None
    matches_workspace: bool = True
    created_at: str = ""
    updated_at: str = ""
    message_count: int = 0
    round_count: int = 0
    usage: dict[str, int] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    tools: dict[str, Any] = field(default_factory=dict)
    skills: dict[str, Any] = field(default_factory=dict)
    size: int = 0
    mtime: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        """转换为可直接 JSON 化的字典（``path`` / ``workspace`` 转为字符串）。"""
        return {
            "session_id": self.session_id,
            "path": str(self.path),
            "scope": self.scope,
            "workspace": str(self.workspace) if self.workspace is not None else None,
            "matches_workspace": self.matches_workspace,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "message_count": self.message_count,
            "round_count": self.round_count,
            "usage": dict(self.usage),
            "metadata": dict(self.metadata),
            "tools": dict(self.tools),
            "skills": dict(self.skills),
            "size": self.size,
            "mtime": self.mtime,
        }

    def to_json(self, *, indent: int | None = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent, default=str)


def _same_workspace(recorded: Path | None, workspace: str | Path) -> bool:
    """``context_matches_workspace()`` 的纯内存版本（``recorded`` 已由 ``Session`` 解析）。

    记录为 ``None``（旧格式 / 未指定）→ 按「运行时继承当前工作区」返回 ``True``。
    """
    if recorded is None:
        return True
    try:
        current = Path(workspace).expanduser().resolve()
    except OSError:  # pragma: no cover - 兜底（例如坏掉的符号链接）
        current = Path(workspace).expanduser()
    return Path(recorded) == current


def _file_stat(path: Path) -> tuple[int, float]:
    """返回 ``(size, mtime)``；文件读不到时返回 ``(0, 0.0)``（不抛异常）。"""
    try:
        stat = path.stat()
    except OSError:
        return 0, 0.0
    return stat.st_size, stat.st_mtime


def _sessions_in_file(path: str | Path) -> list[Session]:
    """读出一个上下文文件里的全部 ``Session``（只读；坏文件 / 非法数据返回空列表）。"""
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    sessions = payload.get("sessions") if isinstance(payload, dict) else None
    if not isinstance(sessions, dict):
        return []

    parsed: list[Session] = []
    for session_id, value in sessions.items():
        if isinstance(value, dict) and "role" in value:
            continue  # 单项消息，不是会话快照
        try:
            parsed.append(Session.from_dict(str(session_id), value))
        except (TypeError, ValueError):
            continue
    return parsed


def _session_info(
    session: Session,
    path: Path,
    scope: str,
    *,
    workspace: str | Path,
) -> SessionInfo:
    """把 ``Session`` 与其所在文件整理成 ``SessionInfo``。"""
    size, mtime = _file_stat(path)
    return SessionInfo(
        session_id=session.session_id,
        path=path,
        scope=scope,
        workspace=session.workspace,
        matches_workspace=_same_workspace(session.workspace, workspace),
        created_at=session.created_at,
        updated_at=session.updated_at,
        message_count=len(session.messages()),
        round_count=len(session.rounds),
        usage=dict(session.usage),
        metadata=dict(session.metadata),
        tools=dict(session.tools),
        skills=dict(session.skills),
        size=size,
        mtime=mtime,
    )


def _sort_infos(infos: list[SessionInfo]) -> list[SessionInfo]:
    """按 ``updated_at`` 从新到旧排序（其次 ``session_id``），返回同一个列表。"""
    infos.sort(key=lambda info: (info.updated_at, info.session_id), reverse=True)
    return infos


def list_sessions(
    workspace: str | Path,
    *,
    project_dir: str | Path | None = None,
    dirname: str | Path = DEFAULT_SESSION_DIRNAME,
    include_project: bool = True,
    only_current_workspace: bool = False,
    dedupe: bool = True,
    priority: str = DEFAULT_SESSION_PRIORITY,
) -> list[SessionInfo]:
    """列出**全部会话**的只读摘要（不载入内容、不创建任何文件）。

    扫描 ``priority`` 顺序下的会话目录（默认工作区在前、项目根目录在后）里的 ``*.json``，
    把每个文件里的每个会话整理成一个 ``SessionInfo``：

    - ``include_project=False``：只列工作区目录；``True``（默认）额外列项目根目录；
    - ``only_current_workspace=True``：只保留 ``matches_workspace`` 为真的会话（口径与
      ``find_context()`` 的项目级回退比对一致，即不把别的工作区的会话算进来）；
    - ``dedupe=True``（默认）：同一个 ``session_id`` 在工作区 / 项目根目录各有一份副本时，
      只保留优先级高的那一份（默认工作区副本，见 ``scope``）；``False`` 时两份都返回；
    - 排序：``updated_at`` 从新到旧，其次 ``session_id``；
    - 容错：文件损坏 / 不是 ``ContextStore`` 格式 / 单个会话数据非法 → 跳过该条，不抛异常；
    - 需要「仅仅是文件路径」（不解析内容）时用 ``find_context_jsons()``；
    - ``SessionInfo.to_dict()`` / ``to_json()`` 可直接交给 Web / TUI 渲染。

    只读操作，不会创建目录或文件。
    """
    workspace_dir = Path(workspace).expanduser() / dirname
    directories = session_search_dirs(
        workspace, project_dir=project_dir, dirname=dirname, priority=priority
    )
    if not include_project:
        directories = [directory for directory in directories if directory == workspace_dir]

    infos: list[SessionInfo] = []
    seen: set[str] = set()
    for directory in directories:
        if not directory.is_dir():
            continue
        scope = (
            SESSION_SCOPE_WORKSPACE if directory == workspace_dir else SESSION_SCOPE_PROJECT
        )
        files = [path for path in directory.glob("*.json") if path.is_file()]
        files.sort(key=lambda path: _file_stat(path)[1], reverse=True)
        for path in files:
            for session in _sessions_in_file(path):
                if dedupe and session.session_id in seen:
                    continue
                info = _session_info(session, path, scope, workspace=workspace)
                if only_current_workspace and not info.matches_workspace:
                    continue
                infos.append(info)
                seen.add(session.session_id)
    return _sort_infos(infos)


# ----------------------------------------------------------------------
# 事件聚合：把 AgentEvent 还原成「用户能看到、能继续处理的响应」
# ----------------------------------------------------------------------


@dataclass
class AgentResponse:
    """``ResponseCollector`` 从事件流里聚合出的 Agent 响应。

    ``text`` / ``reasoning`` 取「最后一次完成的 turn」的正文与思维链（也就是最终回答）；
    需要完整流式文本时用 ``streamed_text``。``tool_calls`` / ``tool_results`` 汇总本次
    请求内所有工具轮次。
    """

    text: str = ""
    reasoning: str = ""
    streamed_text: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    tool_results: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)
    session_id: str = DEFAULT_SESSION_ID
    events: list[AgentEvent] = field(default_factory=list)
    result: AgentResult | None = None

    @property
    def ok(self) -> bool:
        """是否拿到结构化的 ``AgentResult``（事件正常走完时为 ``True``）。"""
        return self.result is not None

    def to_dict(self, *, include_events: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "session_id": self.session_id,
            "text": self.text,
            "reasoning": self.reasoning,
            "tool_calls": self.tool_calls,
            "tool_results": self.tool_results,
            "usage": self.usage,
        }
        if include_events:
            payload["events"] = [dict(event) for event in self.events]
        return payload

    def to_json(self, *, include_events: bool = False, indent: int | None = 2) -> str:
        return json.dumps(self.to_dict(include_events=include_events),
                          ensure_ascii=False, indent=indent, default=str)


def _assistant_from(message: Any) -> AssistantMessage | None:
    return message if isinstance(message, AssistantMessage) else None


class ResponseCollector(AgentEventListener):
    """订阅 ``EventBus`` 的全部事件，聚合成 ``AgentResponse``。

    为什么是同步回调：``AgentEngine.run()`` / ``stream()`` 通过 ``EventBus.emit_sync()``
    派发事件，同步回调会被立即执行；异步回调在存在运行中的事件循环时只会被
    ``create_task`` 而不会被等待，聚合结果因此不可靠。``EventBus.emit()``（异步引擎）
    同样兼容同步回调，所以本监听器对四种入口都有效。

    事件到响应的映射：

    ============================  =================================================
    事件                            聚合内容
    ============================  =================================================
    ``message_update``             text_delta -> 正文增量；reasoning_delta -> 思维链
    ``tool_execution_start``       记录一次工具调用（id / name / args）
    ``tool_execution_update``      记录工具执行中的 partial_result
    ``tool_execution_end``         记录工具结果与 is_error
    ``message_end``                兜底记录最终 assistant 消息（run 模式无增量）
    ``turn_end``                   结束当前 turn，并把该 turn 的正文定为最终回答
    ``agent_end``                  兜底：从 messages 里取最后一个 assistant 文本
    ============================  =================================================
    """

    def __init__(self, *, keep_events: bool = True) -> None:
        self.keep_events = keep_events
        self._unsubscribe: list[Callable[[], None]] = []
        self.result: AgentResult | None = None
        self.reset()

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def reset(self) -> None:
        """清空一次请求的聚合状态（每次 ``send()`` 前调用）。"""
        self._turn_text: list[str] = []
        self._turn_reasoning: list[str] = []
        self._streamed_text: list[str] = []
        self._final_text: str = ""
        self._final_reasoning: str = ""
        self._tool_calls: dict[str, dict[str, Any]] = {}
        self._tool_results: dict[str, dict[str, Any]] = {}
        self._events: list[AgentEvent] = []
        self.result = None

    def attach(self, bus: EventBus) -> list[Callable[[], None]]:
        """注册为总线监听器，返回可用于取消订阅的回调列表。"""
        self.detach()
        self._unsubscribe.append(bus.add_listener(self))
        return list(self._unsubscribe)

    def detach(self) -> None:
        for unsubscribe in self._unsubscribe:
            unsubscribe()
        self._unsubscribe.clear()

    # ------------------------------------------------------------------
    # 结果读取
    # ------------------------------------------------------------------

    @property
    def text(self) -> str:
        if self._final_text:
            return self._final_text
        if self._streamed_text:
            return "".join(self._streamed_text)
        if self.result is not None:
            return self.result.text
        return ""

    @property
    def reasoning(self) -> str:
        if self._final_reasoning:
            return self._final_reasoning
        if self.result is not None and self.result.response is not None:
            return self.result.response.reasoning_content
        return ""

    @property
    def streamed_text(self) -> str:
        return "".join(self._streamed_text)

    @property
    def tool_calls(self) -> list[dict[str, Any]]:
        return list(self._tool_calls.values())

    @property
    def tool_results(self) -> list[dict[str, Any]]:
        return list(self._tool_results.values())

    def snapshot(
        self,
        *,
        session_id: str = DEFAULT_SESSION_ID,
        usage: dict[str, int] | None = None,
        result: AgentResult | None = None,
    ) -> AgentResponse:
        """生成不可变的响应快照，供调用方读取或序列化。"""
        collected = result if result is not None else self.result
        resolved_usage = dict(usage or (collected.usage if collected is not None else {}))
        return AgentResponse(
            text=self.text,
            reasoning=self.reasoning,
            streamed_text=self.streamed_text,
            tool_calls=self.tool_calls,
            tool_results=self.tool_results,
            usage=resolved_usage,
            session_id=session_id,
            events=list(self._events) if self.keep_events else [],
            result=collected,
        )


    # ------------------------------------------------------------------
    # AgentEventListener 接口
    # ------------------------------------------------------------------

    def on_event(self, event: AgentEvent) -> None:  # type: ignore[override]
        """同步接收全部事件；分派规则见类文档字符串。"""
        event_type = event.get("type") if isinstance(event, dict) else None

        if self.keep_events:
            self._events.append(event)

        if event_type == EVENT_TURN_START:
            self._turn_text.clear()
            self._turn_reasoning.clear()
        elif event_type == EVENT_MESSAGE_UPDATE:
            self._on_message_update(event)
        elif event_type == EVENT_MESSAGE_END:
            self._capture_final(event.get("message"))
        elif event_type == EVENT_TURN_END:
            self._on_turn_end(event)
        elif event_type == EVENT_AGENT_END:
            self._on_agent_end(event)
        elif event_type == EVENT_TOOL_START:
            self._on_tool_start(event)
        elif event_type == EVENT_TOOL_UPDATE:
            self._on_tool_update(event)
        elif event_type == EVENT_TOOL_END:
            self._on_tool_end(event)

    # ------------------------------------------------------------------
    # 事件处理细节
    # ------------------------------------------------------------------

    def _on_message_update(self, event: AgentEvent) -> None:
        update = event.get("assistant_message_event") or {}
        kind = update.get("type")
        text = update.get("text") or ""

        if kind == "reasoning_delta":
            self._turn_reasoning.append(text)
        elif kind == "text_delta":
            self._turn_text.append(text)
            self._streamed_text.append(text)
        else:
            # 兜底：没有 assistant_message_event 时从 message 里解析增量。
            message = _assistant_from(event.get("message"))
            if message is not None and message.reasoning_content:
                self._turn_reasoning.append(message.reasoning_content)
            if message is not None and message.content:
                self._turn_text.append(message.content)
                self._streamed_text.append(message.content)

    def _on_turn_end(self, event: AgentEvent) -> None:
        # 该 turn 有流式增量时，以增量拼出的文本作为最终回答。
        if self._turn_text:
            self._final_text = "".join(self._turn_text)
        if self._turn_reasoning:
            self._final_reasoning = "".join(self._turn_reasoning)
        # 没有增量（run 模式）时，用 turn_end 携带的 assistant 消息兜底。
        self._capture_final(event.get("message"))
        self._record_message_tool_results(event.get("tool_results") or [])
        self._turn_text.clear()
        self._turn_reasoning.clear()

    def _capture_final(self, message: Any) -> None:
        assistant = _assistant_from(message)
        if assistant is None:
            return
        if assistant.content:
            self._final_text = assistant.content
        if assistant.reasoning_content:
            self._final_reasoning = assistant.reasoning_content

    def _on_agent_end(self, event: AgentEvent) -> None:
        # 兜底：messages 是完整执行上下文，最后一个 assistant 文本即最终回答。
        for message in reversed(event.get("messages") or []):
            assistant = _assistant_from(message)
            if assistant is not None and assistant.content:
                self._final_text = assistant.content
                if assistant.reasoning_content:
                    self._final_reasoning = assistant.reasoning_content
                break

    def _on_tool_start(self, event: AgentEvent) -> None:
        call_id = str(event.get("tool_call_id") or "")
        self._tool_calls[call_id] = {
            "id": call_id,
            "name": event.get("tool_name") or "",
            "arguments": event.get("args"),
            "partial_result": None,
        }
        self._tool_results.setdefault(call_id, {
            "tool_call_id": call_id,
            "tool_name": event.get("tool_name") or "",
            "content": None,
            "is_error": None,
        })

    def _on_tool_update(self, event: AgentEvent) -> None:
        call_id = str(event.get("tool_call_id") or "")
        entry = self._tool_calls.get(call_id)
        if entry is not None:
            entry["partial_result"] = event.get("partial_result")

    def _on_tool_end(self, event: AgentEvent) -> None:
        call_id = str(event.get("tool_call_id") or "")
        self._tool_results[call_id] = {
            "tool_call_id": call_id,
            "tool_name": event.get("tool_name") or "",
            "content": event.get("result"),
            "is_error": bool(event.get("is_error")),
        }

    def _record_message_tool_results(self, results: Iterable[Any]) -> None:
        for item in results:
            call_id = str(getattr(item, "tool_call_id", "") or "")
            self._tool_results[call_id] = {
                "tool_call_id": call_id,
                "tool_name": getattr(item, "tool_name", "") or "",
                "content": getattr(item, "content", None),
                "is_error": bool(getattr(item, "is_error", False)),
            }


# ----------------------------------------------------------------------
# Agent 门面：初始化 / 注册 / 用户消息入口 / 事件响应
# ----------------------------------------------------------------------


class AgentRunner:
    """CLI / TUI / Web 等宿主程序访问 Agent 的统一入口。

    构造时一次性完成四件事：

    1. **Agent 初始化**：``config.yaml`` -> ``ConfigStore`` -> ``LLMConfig`` -> ``LLMClient``；
       也可以直接注入 ``client`` / ``llm_config``（便于测试或宿主程序复用连接）。
    2. **上下文存储**：``ContextStore`` 保存 ``Session``（消息 + 工作区 + 工具/Skill 配置 +
       token 用量）。**构造过程主动查找会话，但不载入内容、不创建文件**：

       - 查找优先级：显式 ``session_id`` / ``context_path`` > 按 ``session_priority``
         查找（**默认工作区优先**，工作区里没有才回退项目根目录；回退时比对文件里记录的
         ``workspace``，不是当前工作区的会话视为未命中）> 新的 ``uuid4``；命中的文件记入
         ``found_context_path``；
       - 继续已有会话要**显式**调用 ``load_session()``（或 ``load_context(path)``）；
         ``has_unloaded_session`` 为真时发送 / 落盘会先报错，避免覆盖既有历史；
       - 开始新对话要**显式**调用 ``create_context()``：未指定 ``session_id`` 时每次都会
         生成新的 ``uuid4``，因此不会覆盖 / 重置其它会话；
       - 落盘通过 ``save_context()``，或 ``close()``（仅当本次确实有改动时）完成，一式两份
         写入项目根目录与工作区下的 ``.agent/sessions/<session_id>.json``。
       - 列出全部会话用 ``list_sessions()``：只读摘要（``SessionInfo``：id / 文件路径 /
         位置 / 时间 / 消息数 / 轮数 / usage / 元数据），不载入内容、不创建文件。

    3. **Tool 注册**：内置 ``read_file`` / ``list_files`` / ``write_file``（可选 ``bash``）
       注册进 ``ToolRegistry``，并通过 ``extra_tools`` 扩展；``requires_approval=True``
       的工具（``bash``）在 ``ToolRegistry`` 里通过会话的 ``approve`` 回调授权 ——
       本类总会传入一个回调（``create_approve_callback()``），``auto_approve=True`` 时
       回调直接返回 ``True``（不提示、不询问），否则提示风险并询问用户确认。
    4. **Skill 注册**：``SkillRegistry`` 发现 SKILL.md -> 写入 ``session.skills`` ->
       注册 ``load_skill`` / ``load_reference`` 工具 -> 索引注入 system prompt。

    随后用 ``send()`` / ``asend()`` 发送用户消息，用 ``stream()`` / ``astream()`` 流式消费；
    响应内容由 ``ResponseCollector`` 从 ``EventBus`` 事件里聚合，读取 ``response.text`` 即可。
    输入输出完全由宿主决定，本类不做任何 CLI 假设。
    """

    def __init__(
        self,
        *,
        # --- Agent 初始化 ---
        client: Any | None = None,
        llm_config: LLMConfig | None = None,
        config_store: ConfigStore | None = None,
        config_path: str | Path | None = None,
        # --- 会话 / 上下文存储 ---
        session_id: str | None = None,
        workspace: str | Path | None = None,
        store: ContextStore | None = None,
        persist: bool = True,
        reuse_existing: bool = True,
        session_priority: str = DEFAULT_SESSION_PRIORITY,
        project_dir: str | Path | None = None,
        session_dirname: str | Path = DEFAULT_SESSION_DIRNAME,
        context_path: str | Path | None = None,
        # --- system prompt / 额外上下文 ---
        system_prompt: MessageInputs | str | None = None,
        context: MessageInputs | None = None,
        # --- Tool 注册 ---
        include_builtin_tools: bool = True,
        enable_bash: bool = False,
        bash_timeout: float = 30.0,
        extra_tools: Iterable[Tool] = (),
        tool_enabled: Iterable[str] | None = None,
        tool_disabled: Iterable[str] | None = None,
        # --- 工具授权（requires_approval，例如 bash）---
        auto_approve: bool = False,
        # --- Skill 注册 ---
        enable_skills: bool = True,
        skill_tools: bool = True,
        system_skill_dirs: Iterable[str | Path] | None = None,
        project_skill_dirs: Iterable[str | Path] | None = None,
        # --- 引擎 / 事件 ---
        max_tool_rounds: int = DEFAULT_MAX_TOOL_ROUNDS,
        events_bus: EventBus | None = None,
        keep_events: bool = True,
        save_on_close: bool = True,
        **engine_kwargs: Any,
    ) -> None:
        self.workspace = (
            Path(workspace).expanduser().resolve() if workspace is not None else Path.cwd()
        )
        self.persist = persist
        self.reuse_existing = reuse_existing
        # 会话查找优先级：默认工作区优先（workspace -> project）。
        self.session_priority = _normalize_session_priority(session_priority)
        self.project_dir = (
            Path(project_dir).expanduser() if project_dir is not None else project_root()
        )
        self.session_dirname = Path(session_dirname)
        # 显式单文件路径：给定后只读写这一份，不再一式两份。
        self.context_path = (
            Path(context_path).expanduser() if context_path is not None else None
        )
        self.save_on_close = save_on_close
        self.loaded_context_paths: list[Path] = []
        self.saved_context_paths: list[Path] = []
        # 是否有未落盘的改动（决定 close() 是否写文件）。
        self._dirty = False
        # 构造阶段主动查找到的上下文文件（None = 没有查到，按新会话处理）。
        self.found_context_path: Path | None = None
        # 「已绑定到磁盘上的既有会话、但尚未显式载入」时记录其路径：此时发送 / 落盘会
        # 覆盖既有历史，因此由 _begin_turn() / save_context() 兜底拦截。
        self._unloaded_session_file: Path | None = None
        # 本实例的会话 ID：显式传入则沿用；否则先放 uuid4，随后由 _lookup_session() 决定
        # （查找到工作区既有会话时会改写成该会话的 ID）。
        self.session_id = str(session_id) if session_id is not None else new_session_id()

        # 工具授权回调：requires_approval=True 的工具（如 bash）执行前会经它确认。
        # auto_approve=True 时回调直接返回 True（不提示风险、不询问用户）；否则提示风险
        # 并询问用户确认。无论哪种模式都会把回调绑定到会话（ToolRegistry 读取
        # session.approve），因此不会留空导致 PermissionError。
        self.auto_approve = bool(auto_approve)
        self.approve: ApproveFn = create_approve_callback(auto_approve=self.auto_approve)

        # 1) Agent 初始化：注入的 client 优先，其次显式 llm_config，最后读配置文件。
        self.config_store = config_store
        self.llm_config = llm_config
        if client is not None:
            self.client = client
            if self.llm_config is None:
                self.llm_config = getattr(client, "config", None)
        else:
            if self.config_store is None:
                self.config_store = ConfigStore(config_path)
            if self.llm_config is None:
                self.llm_config = self.config_store.load(create=False).llm
            self.client = LLMClient(self.llm_config)

        # 2) 会话 / 上下文存储：构造阶段**主动查找**会话（决定 session_id 与文件位置），
        #    但**不载入内容、不创建文件**；载入必须显式调用 load_session()。
        self.store = store or ContextStore(workspace=self.workspace)
        if self.store.default_workspace is None:
            self.store.default_workspace = self.workspace
        self._lookup_session(session_id)
        self.session = self.store.ensure_session(self.session_id, workspace=self.workspace)
        # 把授权回调绑定到会话：ToolRegistry 通过 session.approve 读取它。
        self.session.approve = self.approve
        if tool_enabled is not None or tool_disabled is not None:
            self.session.configure_tools(enabled=tool_enabled, disabled=tool_disabled)
        self.saved_context_paths = list(self.loaded_context_paths)

        # 3) 事件：先建总线并挂上聚合监听器，后续注册动作也可被监听。
        self.events_bus = events_bus or EventBus()
        self.collector = ResponseCollector(keep_events=keep_events)
        self.collector.attach(self.events_bus)

        # 4) Skill 注册 -> 得到可注入 system prompt 的索引。
        self.skill_index = self._register_skills(
            enable_skills=enable_skills,
            system_skill_dirs=system_skill_dirs,
            project_skill_dirs=project_skill_dirs,
        )

        # 5) Tool 注册（含 Skill 工具）；授权回调同样传给 ToolRegistry。
        self.registry = self._register_tools(
            include_builtin_tools=include_builtin_tools,
            enable_bash=enable_bash,
            bash_timeout=bash_timeout,
            extra_tools=extra_tools,
            skill_tools=skill_tools,
            approve=self.approve,
        )

        # 6) 中间件 + 引擎。
        self.middleware = ContextMiddleware(
            self.client,
            self.store,
            context=self._build_context(system_prompt, context, self.skill_index),
            workspace=self.workspace,
        )
        self.engine = AgentEngine(
            self.client,
            middleware=self.middleware,
            tool_registry=self.registry,
            events_bus=self.events_bus,
            max_tool_rounds=max_tool_rounds,
            **engine_kwargs,
        )
        self._last_result: AgentResult | None = None

    # ------------------------------------------------------------------
    # 会话查找（构造时主动调用）与载入（显式）
    # ------------------------------------------------------------------

    def _lookup_session(self, session_id: str | None) -> None:
        """**主动查找**会话：决定 ``session_id`` 并记录 ``found_context_path``。

        只做只读查找（最多读一次既有 JSON 以确认其中的会话 ID），
        **不会把内容载入 store，也不会创建任何文件**。解析顺序：

        1. 显式 ``session_id``：直接使用，并记录它已存在的副本（若有）；
        2. ``persist=False``：完全不碰磁盘，使用新的 uuid4；
        3. 显式 ``context_path``：存在则用该文件里的会话 ID，否则新建 uuid4；
        4. 注入的 ``store`` 里已有会话：用第一个（内容已在内存里）；
        5. ``reuse_existing=False``：不查找，直接用新的 uuid4；
        6. 否则按 ``session_priority`` 查找（**默认工作区优先**；回退项目级时比对文件里记录的
           工作区，不是当前工作区的会话视为未命中），命中则用其中的会话；
        7. 都没命中：使用新的 uuid4。

        第 1/3/6 种情况命中的文件会记到 ``_unloaded_session_file``：此时 runner 已绑定该
        会话但内容还没载入，必须先 ``load_session()``（继续）或 ``create_context()``
        （新建）再发送消息，避免覆盖既有历史。
        """
        if session_id is not None:
            self.session_id = str(session_id)
            self._bind_found(self._find_session_path(self.session_id))
            return

        if not self.persist:
            self.session_id = new_session_id()
            return

        if self.context_path is not None:
            if self.context_path.is_file():
                self.session_id = self._loaded_session_id(self.context_path)
                self._bind_found(self.context_path)
                return
            self.session_id = new_session_id()
            return

        existing = self.store.sessions()
        if existing:
            # 注入的 store 里已有会话：内容已在内存中，不需要再"载入"。
            self.session_id = str(existing[0])
            return

        if not self.reuse_existing:
            self.session_id = new_session_id()
            return

        found = self.find_context()
        if found is None:
            self.session_id = new_session_id()
            return

        self.session_id = self._loaded_session_id(found)
        self._bind_found(found)

    def _bind_found(self, path: Path | None) -> None:
        """记录「查找到的上下文文件」，并标记其内容尚未载入。"""
        self.found_context_path = path
        self._unloaded_session_file = path

    def _find_session_path(self, session_id: str) -> Path | None:
        """返回该会话已存在的上下文文件（按 ``session_priority``：默认工作区优先）。

        回退到项目根目录时会比对文件里记录的 ``workspace``（见
        ``context_matches_workspace()``）：不是当前工作区的会话不返回。
        """
        if self.context_path is not None:
            return self.context_path if self.context_path.is_file() else None
        candidates = find_context_jsons(
            self.workspace,
            project_dir=self.project_dir,
            dirname=self.session_dirname,
            session_id=session_id,
            priority=self.session_priority,
        )
        return candidates[0] if candidates else None

    @property
    def has_unloaded_session(self) -> bool:
        """是否「已绑定到既有会话文件、但其内容尚未显式载入」。

        为 ``True`` 时发送消息或落盘都会覆盖该会话的既有历史，因此
        ``send()`` / ``stream()`` / ``save_context()`` 会先抛出 ``RuntimeError``，
        要求调用方显式选择：``load_session()``（继续该会话）或 ``create_context()``
        （开始新对话）。
        """
        return self._unloaded_session_file is not None

    def _clear_unloaded_session(self) -> None:
        self._unloaded_session_file = None

    def load_session(
        self,
        session_id: str | None = None,
        *,
        path: str | Path | None = None,
        reuse_existing: bool | None = None,
    ) -> list[Path]:
        """**显式**载入会话并切换过去（构造时只查找、不调用本方法）。

        解析顺序：

        1. ``path``：只载入该文件；文件不存在时返回 ``[]``（不创建文件）；
        2. ``session_id``：在该会话的两份副本中按 ``session_priority`` 载入**优先级最高**
           的那一份（默认工作区副本）；没有副本时返回 ``[]``，但仍把 runner 切到该会话
           （保持空历史，绝不写盘）；
        3. 都不给：``reuse_existing``（默认取构造参数）为 ``True`` 时，优先载入构造时
           查到的会话（``found_context_path``）；没有则按 ``session_priority`` 查找一份
           并载入（可能切换会话）；为 ``False`` 时不做任何查找。

        - ``persist=False`` 时不做磁盘载入，直接返回 ``[]``；
        - 载入成功后清除 ``has_unloaded_session``；
        - 载入后会把工具注册表重新绑定到载入的 ``Session``（``store.load`` 会重建会话）；
        - 返回实际载入的文件路径列表（``[]`` 表示没有载入任何文件）。
        """
        if not self.persist:
            return []

        reuse = self.reuse_existing if reuse_existing is None else bool(reuse_existing)

        if path is not None:
            target = Path(path).expanduser()
            if not target.is_file():
                return []
            self.store.load(target, merge=True)
            self.session_id = self._loaded_session_id(target)
            self.found_context_path = target
            self.loaded_context_paths = [target]
            self._clear_unloaded_session()
            self._rebind_session()
            return [target]

        if session_id is not None:
            self.session_id = str(session_id)
            target = self._find_session_path(self.session_id)
            if target is None:
                # 没有副本：切到该会话但保持空历史，绝不写盘。
                self.loaded_context_paths = []
                self._clear_unloaded_session()
                self._rebind_session()
                return []
            # 优先级最高的一份（默认工作区副本），而不是单纯按 mtime 取最新。
            self.store.load(target, merge=True)
            self.found_context_path = target
            self.loaded_context_paths = [target]
            self._clear_unloaded_session()
            self._rebind_session()
            return [target]

        if not reuse:
            return []

        # 构造时已查到某个会话：优先载入它（“继续构造时发现的会话”），不切换到别的会话。
        pinned = self.found_context_path
        if pinned is not None and pinned.is_file():
            self.store.load(pinned, merge=True)
            self.loaded_context_paths = [pinned]
            self._clear_unloaded_session()
            self._rebind_session()
            return [pinned]

        found = self.find_context()
        if found is None:
            return []

        self.store.load(found, merge=True)
        self.session_id = self._loaded_session_id(found)
        self.found_context_path = found
        self.loaded_context_paths = [found]
        self._clear_unloaded_session()
        self._rebind_session()
        return [found]

    @staticmethod
    def _session_ids_in(path: Path) -> list[str]:
        """读上下文文件里的会话 ID 列表（只读；文件损坏 / 旧格式时返回空列表）。"""
        try:
            payload = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        sessions = payload.get("sessions") if isinstance(payload, dict) else None
        if isinstance(sessions, dict):
            return [str(key) for key in sessions]
        return []

    def _loaded_session_id(self, path: Path) -> str:
        """决定刚载入文件对应的会话 ID。

        约定 ``<session_id>.json`` 的文件名即会话 ID；文件名不是会话 ID 时（例如
        显式传入的 ``context.json``）取文件里的第一个会话，最后才回退到文件名。
        """
        ids = self._session_ids_in(path)
        stem = Path(path).stem
        if stem in ids:
            return stem
        return ids[0] if ids else stem

    def _rebind_session(self) -> None:
        """把当前会话对象与工具注册表重新绑定。

        ``store.load()`` 会重建 ``Session`` 对象，因此每次载入 / 创建上下文后都要
        重新取一次 ``ensure_session()`` 并 ``bind_session()``，否则工具仍指向旧会话。
        重建的会话还要重新绑定授权回调（``session.approve``），否则 ``bash`` 等
        ``requires_approval`` 工具会因缺少 ``approve`` 而报 ``PermissionError``。
        """
        self.session = self.store.ensure_session(self.session_id, workspace=self.workspace)
        self.session.approve = self.approve
        self.registry.bind_session(self.session)




    # ------------------------------------------------------------------
    # 注册实现
    # ------------------------------------------------------------------

    def _register_skills(
        self,
        *,
        enable_skills: bool,
        system_skill_dirs: Iterable[str | Path] | None,
        project_skill_dirs: Iterable[str | Path] | None,
    ) -> str:
        """发现并注册 Skill，返回可注入 system prompt 的 Skill 索引。

        - 默认系统目录：``~/.agent/skills``；
        - 默认项目目录：``<workspace>/.agent/skills``；
        - 目录不存在时 ``discover()`` 会跳过，因此没有 Skill 也不会报错。
        """
        self.skill_registry: SkillRegistry | None = None
        if not enable_skills:
            return ""

        system_dirs = [
            Path(path).expanduser()
            for path in (
                system_skill_dirs if system_skill_dirs is not None else [_SYSTEM_SKILL_DIR]
            )
        ]
        project_dirs = [
            Path(path).expanduser()
            for path in (
                project_skill_dirs
                if project_skill_dirs is not None
                else [self.workspace / _PROJECT_SKILL_DIRNAME]
            )
        ]

        registry = SkillRegistry(system_skill_dirs=system_dirs, project_skill_dirs=project_dirs)
        registry.discover()
        registry.bind_session(self.session)
        self.skill_registry = registry
        return registry.build_prompt_index()

    def _register_tools(
        self,
        *,
        include_builtin_tools: bool,
        enable_bash: bool,
        bash_timeout: float,
        extra_tools: Iterable[Tool],
        skill_tools: bool,
        approve: ApproveFn | None = None,
    ) -> ToolRegistry:
        """把内置工具、Skill 工具与扩展工具统一注册到 ``ToolRegistry``。

        ``approve`` 一并传给 ``ToolRegistry``（写入 ``session.approve``），
        ``requires_approval=True`` 的工具执行前会调用它做授权确认。
        """
        if include_builtin_tools:
            registry = create_builtin_tools(
                include_bash=enable_bash,
                bash_timeout=bash_timeout,
                session=self.session,
                approve=approve,
            )
        else:
            registry = ToolRegistry(session=self.session, approve=approve)

        if skill_tools and self.skill_registry is not None:
            for tool in (self.skill_registry.create_load_skill(),
                         self.skill_registry.create_load_reference()):
                if registry.get(tool.name) is None:
                    registry.register(tool)

        for tool in extra_tools or ():
            if registry.get(tool.name) is not None:
                raise ValueError(f"重复注册工具: {tool.name}")
            registry.register(tool)

        self.session.register_tools(registry.all())
        return registry

    @staticmethod
    def _build_context(
        system_prompt: MessageInputs | str | None,
        context: MessageInputs | None,
        skill_index: str,
    ) -> list[MessageInput]:
        """组装注入到 ``ContextMiddleware`` 的静态上下文。

        顺序：system prompt -> Skill 索引 -> 调用方额外上下文。中间件会把它放在
        「持久化历史」之后、「当前请求」之前（见 ``ContextMiddleware._messages``）。
        """
        items: list[MessageInput] = []
        if system_prompt is not None:
            if isinstance(system_prompt, str):
                items.append(SystemPrompt(content=system_prompt))
            else:
                items.extend(normalize_messages(system_prompt))
        if skill_index:
            items.append(SystemPrompt(content=skill_index))
        if context is not None:
            items.extend(normalize_messages(context))
        return items

    @staticmethod
    def _as_prompt(message: MessageInputs | str) -> MessageInputs:
        """字符串直接视为用户消息，其余类型原样交给引擎。"""
        if isinstance(message, str):
            return UserPrompt(content=message)
        return message


    # ------------------------------------------------------------------
    # 用户消息入口
    # ------------------------------------------------------------------

    def send(
        self,
        message: MessageInputs | str,
        *,
        session_id: str | None = None,
        context: MessageInputs | None = None,
        metadata: dict[str, Any] | None = None,
        tool_registry: ToolRegistry | None = None,
        max_tool_rounds: int | None = None,
        **kwargs: Any,
    ) -> AgentResponse:
        """同步发送一条用户消息，返回事件聚合出的 ``AgentResponse``。

        ``message`` 可以是字符串（自动包成 ``UserPrompt``），也可以是任意
        ``MessageInputs``；``kwargs`` 会透传给 ``AgentEngine.run()``（例如 ``tools``）。
        """
        resolved_session = session_id or self.session_id
        self._begin_turn()
        self._last_result = None
        result = self.engine.run(
            self._as_prompt(message),
            context=context,
            session_id=resolved_session,
            metadata=metadata,
            tool_registry=tool_registry,
            max_tool_rounds=max_tool_rounds,
            **kwargs,
        )
        return self._finish(resolved_session, result)

    async def asend(
        self,
        message: MessageInputs | str,
        *,
        session_id: str | None = None,
        context: MessageInputs | None = None,
        metadata: dict[str, Any] | None = None,
        tool_registry: ToolRegistry | None = None,
        max_tool_rounds: int | None = None,
        **kwargs: Any,
    ) -> AgentResponse:
        """``send()`` 的异步版本（可执行异步工具）。"""
        resolved_session = session_id or self.session_id
        self._begin_turn()
        self._last_result = None
        result = await self.engine.arun(
            self._as_prompt(message),
            context=context,
            session_id=resolved_session,
            metadata=metadata,
            tool_registry=tool_registry,
            max_tool_rounds=max_tool_rounds,
            **kwargs,
        )
        return self._finish(resolved_session, result)

    def stream(
        self,
        message: MessageInputs | str,
        *,
        session_id: str | None = None,
        context: MessageInputs | None = None,
        metadata: dict[str, Any] | None = None,
        tool_registry: ToolRegistry | None = None,
        max_tool_rounds: int | None = None,
        on_chunk: Callable[[AgentStreamChunk], None] | None = None,
        **kwargs: Any,
    ) -> Iterator[AgentStreamChunk]:
        """流式发送用户消息；结束后用 ``response()`` 读取聚合结果。

        ``message_update`` 事件会在迭代过程中通过 ``events_bus`` 派发；调用方既可以
        监听事件做实时输出，也可以消费 ``AgentStreamChunk``。
        """
        resolved_session = session_id or self.session_id
        self._begin_turn()
        self._last_result = None
        for chunk in self.engine.stream(
            self._as_prompt(message),
            context=context,
            session_id=resolved_session,
            metadata=metadata,
            tool_registry=tool_registry,
            max_tool_rounds=max_tool_rounds,
            **kwargs,
        ):
            if chunk.done and chunk.result is not None:
                self._last_result = chunk.result
            if on_chunk is not None:
                on_chunk(chunk)
            yield chunk

    async def astream(
        self,
        message: MessageInputs | str,
        *,
        session_id: str | None = None,
        context: MessageInputs | None = None,
        metadata: dict[str, Any] | None = None,
        tool_registry: ToolRegistry | None = None,
        max_tool_rounds: int | None = None,
        on_chunk: Callable[[AgentStreamChunk], None] | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[AgentStreamChunk]:
        """``stream()`` 的异步版本。"""
        resolved_session = session_id or self.session_id
        self._begin_turn()
        self._last_result = None
        async for chunk in self.engine.astream(
            self._as_prompt(message),
            context=context,
            session_id=resolved_session,
            metadata=metadata,
            tool_registry=tool_registry,
            max_tool_rounds=max_tool_rounds,
            **kwargs,
        ):
            if chunk.done and chunk.result is not None:
                self._last_result = chunk.result
            if on_chunk is not None:
                on_chunk(chunk)
            yield chunk

    def response(
        self,
        session_id: str | None = None,
        *,
        result: AgentResult | None = None,
    ) -> AgentResponse:
        """读取最近一次请求的事件聚合结果。"""
        return self._finish(session_id or self.session_id, result or self._last_result)

    def _begin_turn(self) -> None:
        """开始一次用户请求：清空事件聚合，并标记「有改动」以便 ``close()`` 落盘。

        ``_dirty`` 是「是否需要在 ``close()`` 时写文件」的开关：仅构造 AgentRunner
        不会把它置位，因此不会产生空的上下文 JSON。

        若构造时**查找到**了既有会话却还没显式载入（``has_unloaded_session``），这里会
        先报错：直接发送会让模型在"空历史"上回答，并在落盘时覆盖该会话的既有内容。
        """
        if self.has_unloaded_session:
            raise RuntimeError(
                f"会话 {self.session_id} 在磁盘上已有上下文（{self._unloaded_session_file}）"
                "但尚未载入：请先 load_session() 继续该会话，或 create_context() 开始新对话，"
                "再发送消息（否则会覆盖既有历史）。"
            )
        self.collector.reset()
        self._last_result = None
        self._dirty = True

    def _finish(self, session_id: str, result: AgentResult | None) -> AgentResponse:
        self._last_result = result
        collected = result if result is not None else self.collector.result
        usage = (
            dict(collected.usage)
            if collected is not None and collected.usage
            else self.engine.get_session_usage(session_id)
        )
        return self.collector.snapshot(session_id=session_id, usage=usage, result=result)


    # ------------------------------------------------------------------
    # 上下文 / 事件 / 生命周期
    # ------------------------------------------------------------------

    def get_context(self, session_id: str | None = None) -> list[Any]:
        """返回会话消息历史（provider-neutral 的 ``Message`` 列表）。"""
        return self.engine.get_context(session_id or self.session_id)

    def get_session(self, session_id: str | None = None) -> Session | None:
        """返回 ``Session``（工作区 + 消息 + 工具/Skill 配置 + usage）。"""
        return self.engine.get_session(session_id or self.session_id)

    def get_session_usage(self, session_id: str | None = None) -> dict[str, int]:
        """返回会话累计 token 用量。"""
        return self.engine.get_session_usage(session_id or self.session_id)

    def context_paths(self, session_id: str | None = None) -> list[Path]:
        """返回该会话的上下文存储路径列表。

        - 构造时给了 ``context_path``：只返回这一个文件；
        - 否则：项目根目录 + 工作区两份 ``.agent/sessions/<session_id>.json``
          （两个根目录相同时自动去重）。
        """
        if self.context_path is not None:
            return [self.context_path]
        return session_context_paths(
            session_id or self.session_id,
            self.workspace,
            project_dir=self.project_dir,
            dirname=self.session_dirname,
        )

    def save_context(self, path: str | Path | None = None) -> list[Path]:
        """把上下文写入 JSON，返回实际写入的路径列表。

        默认一式两份（项目根目录 + 工作区，文件名即 UUID）；传入 ``path`` 时只写这一份。
        某个位置不可写（例如只读仓库目录）时会跳过它，只要还有一份成功就返回成功列表；
        全部失败才抛出异常。

        若构造时查找到的既有会话**尚未显式载入**（``has_unloaded_session``），这里会先
        报错，避免用空历史覆盖该会话的既有内容（先 ``load_session()`` 或
        ``create_context()``）。
        """
        if self.has_unloaded_session:
            raise RuntimeError(
                f"会话 {self.session_id} 的既有上下文（{self._unloaded_session_file}）尚未载入："
                "请先 load_session() 继续该会话，或 create_context() 开始新对话，再落盘。"
            )
        targets = (
            [Path(path).expanduser()] if path is not None else self.context_paths()
        )
        written: list[Path] = []
        errors: list[OSError] = []
        for target in targets:
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                self.store.save_session(target, self.session_id)
                written.append(target)
            except OSError as exc:
                errors.append(exc)
        if not written and errors:
            raise errors[0]
        return written

    def load_context(self, path: str | Path | None = None) -> list[Path]:
        """从 JSON 载入上下文，返回实际载入的路径列表。

        - 传入 ``path``：只尝试该文件；
        - 指定了 ``context_path``：只尝试那一份；
        - 否则：在当前会话的两份副本中选**优先级最高**的一份（默认工作区副本）；
        - 缺失则返回空列表（不会创建文件）；
        - 载入成功后清除 ``has_unloaded_session``（内容已进内存，可安全落盘）。
        """
        if path is not None:
            target = Path(path).expanduser()
            if not target.is_file():
                return []
            self.store.load(target, merge=True)
            self._clear_unloaded_session()
            return [target]

        if self.context_path is not None:
            candidates = [self.context_path] if self.context_path.is_file() else []
        else:
            candidates = find_context_jsons(
                self.workspace,
                project_dir=self.project_dir,
                dirname=self.session_dirname,
                session_id=self.session_id,
                priority=self.session_priority,
            )
        if not candidates:
            return []
        target = candidates[0]
        self.store.load(target, merge=True)
        self.found_context_path = target
        self._clear_unloaded_session()
        return [target]

    def find_context(
        self,
        session_id: str | None = None,
        *,
        priority: str | None = None,
    ) -> Path | None:
        """查找已存在的上下文 JSON（只读，不创建任何文件）。

        - ``session_id`` 给定时：只找该会话的两份副本，按 ``priority`` 返回优先级最高的；
        - ``context_path`` 给定时：只检查该文件；
        - 否则：**先扫工作区**，工作区里没有才回退项目根目录，返回优先级最高（默认工作区）、
          其次最新的一份；
        - 回退到项目根目录时比对文件里记录的 ``workspace``：与 ``self.workspace`` 不一致的
          **不是**本次要查找的上下文（项目级是多个工作区共享的），直接返回 ``None``；
        - 找不到返回 ``None``。

        ``priority`` 省略时使用构造参数 ``session_priority``（默认 ``"workspace"``）。
        """
        resolved_priority = self.session_priority if priority is None else priority
        if session_id is not None:
            return find_context_json(
                self.workspace,
                project_dir=self.project_dir,
                dirname=self.session_dirname,
                session_id=session_id,
                priority=resolved_priority,
            )
        if self.context_path is not None:
            return self.context_path if self.context_path.is_file() else None
        return find_context_json(
            self.workspace,
            project_dir=self.project_dir,
            dirname=self.session_dirname,
            priority=resolved_priority,
        )

    def list_sessions(
        self,
        *,
        include_project: bool = True,
        only_current_workspace: bool = False,
        dedupe: bool = True,
        priority: str | None = None,
    ) -> list[SessionInfo]:
        """列出**全部会话**的只读摘要（模块级 ``list_sessions()`` 的便捷入口）。

        列出工作区（默认还含项目根目录）下的每个会话：id / 文件路径 / 位置 / 时间 / 消息数 /
        轮数 / usage / 元数据等（``SessionInfo``）。**只读**：不载入内容、不创建文件。

        - ``persist=False``：纯内存模式不读盘，直接返回 ``[]``；
        - 构造时给了 ``context_path``：只列出该文件里的会话（``scope="context"``）；
        - ``include_project`` / ``only_current_workspace`` / ``dedupe`` 语义见模块级
          ``list_sessions()``；``priority`` 省略时用构造参数 ``session_priority``。
        """
        if not self.persist:
            return []
        if self.context_path is not None:
            target = Path(self.context_path).expanduser()
            if not target.is_file():
                return []
            return _sort_infos(
                [
                    _session_info(
                        session, target, SESSION_SCOPE_CONTEXT, workspace=self.workspace
                    )
                    for session in _sessions_in_file(target)
                ]
            )
        return list_sessions(
            self.workspace,
            project_dir=self.project_dir,
            dirname=self.session_dirname,
            include_project=include_project,
            only_current_workspace=only_current_workspace,
            dedupe=dedupe,
            priority=self.session_priority if priority is None else priority,
        )

    def create_context(
        self,
        session_id: str | None = None,
        *,
        metadata: dict[str, Any] | None = None,
        overwrite: bool = False,
    ) -> list[Path]:
        """**主动**创建一份新的上下文 JSON 并切换过去，返回写入的路径列表。

        这是「创建上下文 / 开始一段新对话」的唯一入口，构造 ``AgentRunner`` 不会创建文件。

        - ``session_id is None``（默认）：生成**新的** ``uuid4`` —— 每次调用都是一段全新
          对话，绝不会复用 / 覆盖其它会话的 ``session_id``；
        - 显式传入 ``session_id``：只创建（或写回）该会话的副本；``overwrite=False`` 时
          已存在的文件保持原内容；
        - 默认一式两份（项目根目录 + 工作区）；构造时给了 ``context_path`` 则只创建那一份。
        """
        target_session = str(session_id) if session_id is not None else new_session_id()
        paths = create_context_json(
            target_session,
            self.workspace,
            path=self.context_path,
            project_dir=self.project_dir,
            dirname=self.session_dirname,
            metadata=metadata,
            overwrite=overwrite,
        )
        self.session_id = target_session
        self.found_context_path = self._find_session_path(self.session_id)
        self.loaded_context_paths = self.load_context()
        self._clear_unloaded_session()
        self._rebind_session()
        self.saved_context_paths = list(paths)
        self._dirty = False
        return paths

    def clear_context(self, session_id: str | None = None) -> None:
        """清空会话历史（工具/Skill 配置与 usage 会随会话一起重建）。"""
        self.middleware.clear_context(session_id or self.session_id)

    def on(self, event_type: str, handler: Callable[[AgentEvent], Any]) -> Callable[[], None]:
        """订阅指定事件类型，返回取消订阅回调（转发到 ``EventBus.on``）。"""
        return self.events_bus.on(event_type, handler)

    def add_listener(self, listener: AgentEventListener) -> Callable[[], None]:
        """订阅全部事件（转发到 ``EventBus.add_listener``）。"""
        return self.events_bus.add_listener(listener)

    def tool_definitions(self) -> list[Any]:
        """返回提供给 LLM 的工具定义（受会话 enable/disable 配置影响）。"""
        return self.registry.definitions()

    def skill_names(self) -> list[str]:
        """返回本会话生效的 Skill 名称。"""
        return self.skill_registry.names() if self.skill_registry is not None else []

    def close(self) -> None:
        """取消事件监听；仅在「本次有改动」时按配置落盘上下文，可重复调用。

        落盘条件：``save_on_close=True`` + ``persist=True`` + ``self.dirty=True``
        （发送过消息，或调用过 ``create_context()``）。因此只构造 AgentRunner 然后
        ``close()`` 不会生成任何上下文 JSON。
        """
        if self.save_on_close and self.persist and self._dirty:
            self.saved_context_paths = self.save_context()
            self._dirty = False
        self.collector.detach()

    @property
    def dirty(self) -> bool:
        """本次运行是否有未落盘的改动（决定 ``close()`` 是否写文件）。"""
        return self._dirty

    def __enter__(self) -> "AgentRunner":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()


# ----------------------------------------------------------------------
# 便捷入口
# ----------------------------------------------------------------------


def build_agent(**kwargs: Any) -> AgentRunner:
    """``AgentRunner`` 的工厂函数（供 CLI / TUI / Web 等宿主复用）。

    等价于 ``AgentRunner(**kwargs)``；单独提供是为了让入口语义更清晰，也方便后续
    在不改变调用方代码的前提下替换实现。
    """
    return AgentRunner(**kwargs)


def run_agent(
    message: MessageInputs | str,
    *,
    stream: bool = False,
    on_delta: Callable[[str], None] | None = None,
    close: bool = True,
    session_id: str | None = None,
    load: bool = False,
    **kwargs: Any,
) -> AgentResponse:
    """一次性入口：构造 Agent -> 发送消息 -> 返回事件聚合响应。

    ``stream=True`` 时用流式引擎，并通过 ``on_delta`` 逐段回调正文增量；
    ``close=True`` 时会调用 ``AgentRunner.close()``（按配置落盘上下文）。

    ``load=True`` 时在发送前**显式**载入会话：``session_id`` 给定时载入该会话，
    否则按 ``reuse_existing`` / ``session_priority`` 查找已有上下文
    （默认 ``load=False``：每次都是全新会话，不查找、不加载）。
    """
    kwargs.setdefault("reuse_existing", load)
    runner = AgentRunner(session_id=session_id, **kwargs)
    try:
        if load:
            runner.load_session(session_id)
        if stream:
            for chunk in runner.stream(message):
                if on_delta is not None and chunk.delta:
                    on_delta(chunk.delta)
        else:
            runner.send(message)
        return runner.response()
    finally:
        if close:
            runner.close()
