"""会话状态：对话的工作区、上下文表示、扩展元数据、工具/Skill 配置和 token 用量。

Context 层用 ``Session`` 表示一次会话的全部状态：

```text
Session
├── session_id      会话 ID
├── workspace       会话工作区（工具执行的根目录）
├── context         上下文表示（provider-neutral 的消息列表）
├── metadata        会话级扩展信息（业务自定义，不存放下面的保留配置）
├── tools           工具配置：本会话注册了哪些工具 + 启用/禁用/选项
├── skills          Skill 配置：本会话注册了哪些 Skill + 启用/禁用/选项
├── usage           token 累计用量：input/output/cache_hit/total
├── approve         工具授权回调
└── cancelled       取消检查回调
```

``tools`` / ``skills`` / ``usage`` 是**独立字段**（不再嵌在 ``metadata`` 中），
``tools`` / ``skills`` 结构一致::

    session.tools = {
        "registered": ["read_file", "bash"],       # 本会话注册（可用）的工具
        "enabled": ["read_file"],                  # 白名单（非空时生效）
        "disabled": ["bash"],                      # 黑名单（优先级更高）
        "options": {"bash": {"timeout": 60}},      # 每个工具的专属选项
        "timeout": 30,                             # 其余键为全局选项
    }
    session.skills = {
        "registered": ["python-code-review"],      # 本会话注册（可用）的 Skill
        "enabled": [...],
        "disabled": [...],
        "options": {...},
    }

``registered`` 只记录“这个会话有哪些工具/Skill”，与 ``enabled`` / ``disabled``
（是否允许使用）解耦：``ToolRegistry`` 注册工具时会自动写入，
``SkillRegistry.bind_session()`` 会写入发现的 Skill；调用方也可以直接用
``Session.register_tools()`` / ``Session.register_skills()`` 登记。

会话还会累计每次 LLM 调用的 token 用量::

    session.usage = {
        "input_tokens": 150,        # 累计输入 token
        "output_tokens": 30,        # 累计输出 token
        "cache_hit_tokens": 45,     # 累计命中的缓存 token
        "total_tokens": 180,        # input + output
    }

``ContextMiddleware`` 每完成一轮 LLM 调用都会调用 ``Session.record_usage()``，
因此 ``session.usage`` 可以直接读取本会话的累计用量，并且随会话 JSON 一起持久化。

兼容旧格式：``tools`` / ``skills`` / ``usage`` 曾嵌在 ``metadata`` 中，构造
``Session`` 或 ``Session.from_dict()`` 读取时会自动迁移到独立字段。

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

# 工具配置的字段名 / JSON 键 / 旧 metadata 键：session.tools。
TOOL_CONFIG_KEY = "tools"
# Skill 配置的字段名 / JSON 键 / 旧 metadata 键：session.skills。
SKILL_CONFIG_KEY = "skills"
# 每种能力配置中记录“本会话注册了哪些工具/Skill”的键。
REGISTERED_KEY = "registered"

# token 用量的字段名 / JSON 键 / 旧 metadata 键：session.usage。
USAGE_KEY = "usage"
# 会话累计的 token 指标；total_tokens 按 input + output 计算，与 LLMClient 一致。
USAGE_METRICS = ("input_tokens", "output_tokens", "cache_hit_tokens", "total_tokens")

# 需要从 metadata 独立出去的保留字段（兼容旧格式时用于迁移）。
RESERVED_KEYS = (TOOL_CONFIG_KEY, SKILL_CONFIG_KEY, USAGE_KEY)

# 不参与“全局选项”合并的保留键：registered / enabled / disabled / options。
_RESERVED_CONFIG_KEYS = frozenset({REGISTERED_KEY, "enabled", "disabled", "options"})


def _capability_map(value: Any) -> dict[str, Any]:
    """把工具/Skill 配置归一化成 dict；非 Mapping 时视为空配置。"""
    return dict(value) if isinstance(value, Mapping) else {}


def empty_usage() -> dict[str, int]:
    """返回全 0 的 token usage，字段与 ``LLMClient`` / ``normalize_usage()`` 一致。"""
    return {metric: 0 for metric in USAGE_METRICS}


def _coerce_usage(value: Any) -> dict[str, int]:
    """把任意 usage 归一化成包含全部指标的 dict（缺失的按 0 处理）。

    ``total_tokens`` 缺失（或为 0）但存在 input/output 时，按 ``input + output``
    补全，与 ``LLMClient`` / ``normalize_usage()`` 保持一致。
    """
    usage = empty_usage()
    if isinstance(value, Mapping):
        for metric in USAGE_METRICS:
            usage[metric] = int(value.get(metric, 0) or 0)
        if not usage["total_tokens"]:
            usage["total_tokens"] = usage["input_tokens"] + usage["output_tokens"]
    return usage


def _usage_delta(usage: Mapping[str, Any] | None) -> dict[str, int]:
    """把一次 LLM 响应的 usage 归一化为可累加的增量。"""
    if not isinstance(usage, Mapping):
        return empty_usage()
    input_tokens = int(usage.get("input_tokens", 0) or 0)
    output_tokens = int(usage.get("output_tokens", 0) or 0)
    cache_hit_tokens = int(usage.get("cache_hit_tokens", 0) or 0)
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_hit_tokens": cache_hit_tokens,
        # 与 LLMClient / normalize_usage 保持一致：total = input + output。
        "total_tokens": input_tokens + output_tokens,
    }



def _normalize_names(values: Any) -> list[str]:
    """把 ``Tool`` / ``SkillMetadata`` / 名称字符串统一成去重后的名称列表。

    支持传入单个对象、单个名称或它们的任意可迭代集合；返回值保留首次出现的顺序，
    因此也可以用来读取已注册列表。无法解析出名称时抛出 ``ValueError``。
    """
    if values is None:
        return []
    if isinstance(values, (str, bytes)) or not hasattr(values, "__iter__"):
        values = [values]
    names: list[str] = []
    for value in values:
        name = getattr(value, "name", None)
        if name is None and isinstance(value, str):
            name = value
        if not name:
            raise ValueError(
                f"无法解析名称: {value!r}（需要 Tool/SkillMetadata 或名称字符串）"
            )
        name = str(name)
        if name not in names:
            names.append(name)
    return names


def utc_now() -> str:
    """返回 UTC ISO8601 时间戳，用于会话创建/更新时间。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Session:
    """一次会话的完整状态：工作区 + 上下文表示 + 元数据 + 工具/Skill 配置 + token 用量。

    它同时就是工具运行上下文（原 ``tools.RuntimeContext`` 已合并到这里）：
    ``workspace`` 为 ``None`` 表示“尚未显式指定”，运行时可以继承工具注册表的
    工作区，``effective_workspace`` 给出实际生效的目录；``metadata`` /
    ``approve`` / ``cancelled`` / ``log()`` / ``is_cancelled()`` 供
    ``ToolRegistry`` 和工具使用。

    工具/Skill 配置与 token 用量是独立字段（不放 ``metadata``）：

    - ``tools`` / ``skills``：本会话注册了哪些工具/Skill（``registered``）、
      启用/禁用列表以及每个工具/Skill 的专属选项；常用入口是
      ``register_tools()`` / ``register_skills()``、``configure_tools()`` /
      ``configure_skills()``、``registered_tools()`` / ``registered_skills()``
      与 ``registrations()``。
    - ``usage``：本会话累计的 token 用量（input / output / cache_hit / total），
      由 ``record_usage()`` 累加、``reset_usage()`` 归零，并随会话 JSON 持久化。

    为兼容旧数据，若 ``metadata`` 中仍带 ``tools`` / ``skills`` / ``usage``，
    构造时会自动迁移到上述字段，并从 ``metadata`` 中移除。
    """

    session_id: str = "default"
    workspace: Path | None = None
    context: list[Message] = field(default_factory=list)
    # 会话级扩展信息（业务自定义；工具/Skill 配置与 usage 使用下面的独立字段）。
    metadata: dict[str, Any] = field(default_factory=dict)
    # 工具配置：registered / enabled / disabled / options 及全局选项。
    tools: dict[str, Any] = field(default_factory=dict)
    # Skill 配置：registered / enabled / disabled / options 及全局选项。
    skills: dict[str, Any] = field(default_factory=dict)
    # token 累计用量：input_tokens / output_tokens / cache_hit_tokens / total_tokens。
    usage: dict[str, int] = field(default_factory=empty_usage)
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
        self.tools = _capability_map(self.tools)
        self.skills = _capability_map(self.skills)
        self.usage = _coerce_usage(self.usage)
        # 兼容旧格式：把 metadata 中的保留键迁移到独立字段。
        self._absorb_reserved(self.metadata)

    def _absorb_reserved(self, values: Mapping[str, Any]) -> None:
        """把保留键（tools/skills/usage）从 ``values`` 取出并写入独立字段。

        ``values`` 会被就地修改（保留键被移除）。工具/Skill 配置按浅合并写入，
        因此旧数据（完整配置）与局部更新都能正确落地。
        """
        tools = values.pop(TOOL_CONFIG_KEY, None)
        if tools is not None:
            merged = dict(self.tools)
            merged.update(_capability_map(tools))
            self.tools = merged
        skills = values.pop(SKILL_CONFIG_KEY, None)
        if skills is not None:
            merged = dict(self.skills)
            merged.update(_capability_map(skills))
            self.skills = merged
        usage = values.pop(USAGE_KEY, None)
        if usage is not None:
            self.usage = _coerce_usage(usage)

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
        """合并会话元数据。

        保留键（``tools`` / ``skills`` / ``usage``）不会留在 ``metadata`` 中，
        而是写入对应独立字段（工具/Skill 配置浅合并），以兼容旧调用方式。
        """
        merged = dict(metadata or {})
        merged.update(values)
        self._absorb_reserved(merged)
        self.metadata.update(merged)
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

    # ------------------------------------------------------------------
    # 工具配置（session.tools）
    # ------------------------------------------------------------------

    def tool_config(self) -> dict[str, Any]:
        """返回 ``session.tools`` 工具配置（副本）。"""
        return self._capability_config(TOOL_CONFIG_KEY)

    def registered_tools(self) -> list[str]:
        """返回本会话注册（可用）的工具名称，顺序即注册顺序。"""
        return _normalize_names(self.tool_config().get(REGISTERED_KEY))

    def register_tools(self, tools: Any) -> "Session":
        """记录本会话注册的工具。

        ``tools`` 可以是 ``Tool`` 对象、工具名，或它们的可迭代集合；重复注册
        不会重复记录，也不会改变 ``enabled`` / ``disabled`` / ``options``。
        """
        return self._register_capabilities(TOOL_CONFIG_KEY, tools)

    def unregister_tools(self, tools: Any) -> "Session":
        """从本会话的已注册工具中移除（接受 ``Tool`` 对象或工具名）。"""
        return self._unregister_capabilities(TOOL_CONFIG_KEY, tools)

    def clear_registered_tools(self) -> "Session":
        """清空已注册工具列表（保留 ``enabled`` / ``disabled`` / ``options``）。"""
        return self._clear_registered(TOOL_CONFIG_KEY)

    def tool_options(self, tool_name: str | None = None) -> dict[str, Any]:
        """返回工具选项；传入工具名时合并该工具的专属配置。

        配置结构::

            session.tools = {
                "registered": ["read_file"],       # 本会话注册的工具
                "enabled": ["read_file"],          # 白名单（非空时生效）
                "disabled": ["bash"],              # 黑名单
                "timeout": 30,                     # 全局选项
                "options": {"bash": {"timeout": 60}},  # 每个工具的选项
            }
        """
        return self._capability_options(TOOL_CONFIG_KEY, tool_name)

    def tool_enabled(self, tool_name: str) -> bool:
        """按会话工具配置判断工具是否启用。"""
        return self._capability_enabled(TOOL_CONFIG_KEY, tool_name)

    def configure_tools(
        self,
        *,
        registered: Iterable[str] | None = None,
        enabled: Iterable[str] | None = None,
        disabled: Iterable[str] | None = None,
        options: Mapping[str, Mapping[str, Any]] | None = None,
        **values: Any,
    ) -> "Session":
        """写入会话级工具配置（合并到 ``session.tools``）。

        ``registered`` 覆盖“本会话注册了哪些工具”，其余参数语义不变。
        """
        return self._configure_capabilities(
            TOOL_CONFIG_KEY,
            registered=registered,
            enabled=enabled,
            disabled=disabled,
            options=options,
            **values,
        )

    # ------------------------------------------------------------------
    # Skill 配置（session.skills）
    # ------------------------------------------------------------------

    def skill_config(self) -> dict[str, Any]:
        """返回 ``session.skills`` Skill 配置（副本）。"""
        return self._capability_config(SKILL_CONFIG_KEY)

    def registered_skills(self) -> list[str]:
        """返回本会话注册（可用）的 Skill 名称，顺序即注册顺序。"""
        return _normalize_names(self.skill_config().get(REGISTERED_KEY))

    def register_skills(self, skills: Any) -> "Session":
        """记录本会话注册的 Skill。

        ``skills`` 可以是 ``SkillMetadata``、Skill 名称，或它们的可迭代集合；
        重复注册不会重复记录。
        """
        return self._register_capabilities(SKILL_CONFIG_KEY, skills)

    def unregister_skills(self, skills: Any) -> "Session":
        """从本会话的已注册 Skill 中移除（接受 ``SkillMetadata`` 或名称）。"""
        return self._unregister_capabilities(SKILL_CONFIG_KEY, skills)

    def clear_registered_skills(self) -> "Session":
        """清空已注册 Skill 列表（保留 ``enabled`` / ``disabled`` / ``options``）。"""
        return self._clear_registered(SKILL_CONFIG_KEY)

    def skill_options(self, skill_name: str | None = None) -> dict[str, Any]:
        """返回 Skill 选项；传入名称时合并该 Skill 的专属配置。"""
        return self._capability_options(SKILL_CONFIG_KEY, skill_name)

    def skill_enabled(self, skill_name: str) -> bool:
        """按会话 Skill 配置判断 Skill 是否启用。"""
        return self._capability_enabled(SKILL_CONFIG_KEY, skill_name)

    def configure_skills(
        self,
        *,
        registered: Iterable[str] | None = None,
        enabled: Iterable[str] | None = None,
        disabled: Iterable[str] | None = None,
        options: Mapping[str, Mapping[str, Any]] | None = None,
        **values: Any,
    ) -> "Session":
        """写入会话级 Skill 配置（合并到 ``session.skills``）。"""
        return self._configure_capabilities(
            SKILL_CONFIG_KEY,
            registered=registered,
            enabled=enabled,
            disabled=disabled,
            options=options,
            **values,
        )

    # ------------------------------------------------------------------
    # Token usage（session.usage）
    # ------------------------------------------------------------------

    def record_usage(self, usage: Mapping[str, Any] | None) -> "Session":
        """累加一次 LLM 调用的 token 用量到本会话（``session.usage``）。

        ``input_tokens`` / ``output_tokens`` / ``cache_hit_tokens`` 逐次累加，
        ``total_tokens`` 按 ``input + output`` 累加（与 ``LLMClient`` 一致）。
        传入 ``None`` 或空 usage 不会改变累计值。
        """
        current = _coerce_usage(self.usage)
        delta = _usage_delta(usage)
        for metric in USAGE_METRICS:
            current[metric] += delta[metric]
        self.usage = current
        return self.touch()

    def reset_usage(self) -> "Session":
        """把本会话累计的 token 用量归零（``session.usage``）。"""
        self.usage = empty_usage()
        return self.touch()

    # ------------------------------------------------------------------
    # 注册总览
    # ------------------------------------------------------------------

    def registrations(self) -> dict[str, list[str]]:
        """返回本会话注册的工具与 Skill：``{"tools": [...], "skills": [...]}``。"""
        return {"tools": self.registered_tools(), "skills": self.registered_skills()}

    # ------------------------------------------------------------------
    # 工具/Skill 配置的通用实现
    # ------------------------------------------------------------------

    def _capability_config(self, config_key: str) -> dict[str, Any]:
        """读取工具/Skill 配置字段（副本）；非 Mapping 时视为空配置。"""
        return _capability_map(getattr(self, config_key, None))

    def _set_capability_config(self, config_key: str, config: Mapping[str, Any]) -> "Session":
        """写入 ``session.tools`` / ``session.skills`` 字段并刷新 ``updated_at``。"""
        setattr(self, config_key, dict(config))
        return self.touch()

    def _capability_enabled(self, config_key: str, name: str) -> bool:
        """黑名单优先，其次是白名单（非空时生效），否则默认启用。"""
        config = self._capability_config(config_key)
        disabled = config.get("disabled")
        if isinstance(disabled, (list, tuple, set, frozenset)) and name in disabled:
            return False
        enabled = config.get("enabled")
        if isinstance(enabled, (list, tuple, set, frozenset)) and enabled:
            return name in enabled
        return True

    def _capability_options(self, config_key: str, name: str | None = None) -> dict[str, Any]:
        """合并全局选项与某个工具/Skill 的专属选项。"""
        config = self._capability_config(config_key)
        options = {
            key: value
            for key, value in config.items()
            if key not in _RESERVED_CONFIG_KEYS
        }
        if name is not None:
            per_item = config.get("options")
            if isinstance(per_item, Mapping):
                specific = per_item.get(name)
                if isinstance(specific, Mapping):
                    options.update(specific)
        return options

    def _configure_capabilities(
        self,
        config_key: str,
        *,
        registered: Iterable[str] | None = None,
        enabled: Iterable[str] | None = None,
        disabled: Iterable[str] | None = None,
        options: Mapping[str, Mapping[str, Any]] | None = None,
        **values: Any,
    ) -> "Session":
        """合并写入 ``session.tools`` / ``session.skills`` 的注册/启用/禁用/选项配置。"""
        config = self._capability_config(config_key)
        if registered is not None:
            config[REGISTERED_KEY] = _normalize_names(registered)
        if enabled is not None:
            config["enabled"] = _normalize_names(enabled)
        if disabled is not None:
            config["disabled"] = _normalize_names(disabled)
        if options is not None:
            merged: dict[str, Any] = dict(config.get("options") or {})
            for name, value in options.items():
                specific = dict(merged.get(name) or {})
                specific.update(value or {})
                merged[str(name)] = specific
            config["options"] = merged
        config.update(values)
        return self._set_capability_config(config_key, config)

    def _register_capabilities(self, config_key: str, values: Any) -> "Session":
        """把名称追加进 ``registered``（保序、去重）。"""
        names = _normalize_names(values)
        if not names:
            return self
        config = self._capability_config(config_key)
        current = _normalize_names(config.get(REGISTERED_KEY))
        for name in names:
            if name not in current:
                current.append(name)
        config[REGISTERED_KEY] = current
        return self._set_capability_config(config_key, config)

    def _unregister_capabilities(self, config_key: str, values: Any) -> "Session":
        """从 ``registered`` 中移除名称（保序）。"""
        names = set(_normalize_names(values))
        if not names:
            return self
        config = self._capability_config(config_key)
        config[REGISTERED_KEY] = [
            name for name in _normalize_names(config.get(REGISTERED_KEY)) if name not in names
        ]
        return self._set_capability_config(config_key, config)

    def _clear_registered(self, config_key: str) -> "Session":
        """清空 ``registered``，保留同一配置中的其它键。"""
        config = self._capability_config(config_key)
        config[REGISTERED_KEY] = []
        return self._set_capability_config(config_key, config)

    def tool_context(
        self,
        *,
        metadata: Mapping[str, Any] | None = None,
        approve: ApproveFn | None = None,
        cancelled: CancelFn | None = None,
    ) -> "Session":
        """返回本次工具执行的会话视图。

        视图与源会话共享 ``session_id``、工作区、工具/Skill 配置和回调，并把调用方
        ``metadata``（例如引擎本次调用的元数据）合并进会话元数据后交给工具使用；
        会话元数据优先，并且始终写入当前 ``session_id``。保留键
        （``tools`` / ``skills`` / ``usage``）不再通过 ``metadata`` 传递，避免覆盖
        会话自身的配置。
        """
        merged = dict(metadata or {})
        merged.update(self.metadata)
        merged["session_id"] = self.session_id
        for key in RESERVED_KEYS:
            merged.pop(key, None)
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
        """转换为可 JSON 序列化的会话快照。

        ``tools`` / ``skills`` / ``usage`` 是会话快照的顶层字段，不再嵌在
        ``metadata`` 中（version 3 格式）。
        """
        return {
            "session_id": self.session_id,
            "workspace": str(self.workspace) if self.workspace is not None else None,
            "metadata": dict(self.metadata),
            "tools": dict(self.tools),
            "skills": dict(self.skills),
            "usage": dict(self.usage),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "messages": [message_to_dict(message) for message in self.context],
        }

    @classmethod
    def from_dict(cls, session_id: str, payload: Any) -> "Session":
        """从 ``to_dict()`` 结果恢复会话。

        同时兼容旧格式：

        - version 1：``payload`` 直接是消息列表，``workspace`` 为 ``None``
          （运行时继承工具注册表的工作区）；
        - version 2：``tools`` / ``skills`` / ``usage`` 嵌在 ``metadata`` 中，
          读取时自动迁移到独立字段。
        """
        if isinstance(payload, Session):
            return payload if payload.session_id == session_id else replace(payload, session_id=session_id)
        if isinstance(payload, (list, tuple)):
            return cls(session_id=session_id, context=list(payload))
        if not isinstance(payload, dict):
            raise ValueError(f"会话数据必须是对象或消息列表: {type(payload).__name__}")
        workspace = payload.get("workspace")
        metadata = dict(payload.get("metadata") or {})
        # 顶层独立字段优先；metadata 中的旧键留给 __post_init__ 迁移。
        for key in RESERVED_KEYS:
            if payload.get(key) is not None:
                metadata.pop(key, None)
        return cls(
            session_id=str(payload.get("session_id") or session_id),
            workspace=None if workspace is None else Path(workspace),
            context=list(payload.get("messages") or []),
            metadata=metadata,
            tools=payload.get(TOOL_CONFIG_KEY),
            skills=payload.get(SKILL_CONFIG_KEY),
            usage=payload.get(USAGE_KEY),
            created_at=payload.get("created_at") or utc_now(),
            updated_at=payload.get("updated_at") or utc_now(),
        )


__all__ = [
    "Session",
    "ApproveFn",
    "CancelFn",
    "TOOL_CONFIG_KEY",
    "SKILL_CONFIG_KEY",
    "REGISTERED_KEY",
    "USAGE_KEY",
    "USAGE_METRICS",
    "RESERVED_KEYS",
    "empty_usage",
    "utc_now",
]

