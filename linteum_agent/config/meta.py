"""Schema metadata for config CLIs and UIs.

The config layer is the single source of truth for *what* can be configured.
This module turns the dataclasses in :mod:`linteum_agent.config.schema` into a
transport-friendly description so future CLI/TUI/Web settings pages can
render forms without hard-coding field lists:

    {"version": 1, "sections": [
        {"key": "agent.llm", "title": "模型", "fields": [
            {"key": "agent.llm.model", "type": "str", "default": "gpt-4o-mini",
             "title": "模型 ID", "apply_scope": "new_session", ...}
        ]}
    ]}

Metadata is intentionally descriptive, not authoritative: defaults and types
come from the dataclasses; this module only adds titles, enums, ranges,
secret markers and the apply scope.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, fields, is_dataclass
from typing import Any

from ..runtime.llm.config import LLMConfig, PROVIDER_DEFAULTS
from .schema import (
    CONFIG_VERSION,
    AppConfig,
    ConfigError,
    _coerce,
    default_config,
)

#: Display title for each section (dotted prefix).
SECTION_TITLES: dict[str, str] = {
    "agent.llm": "模型",
    "agent.profiles": "模型 Profiles",
    "agent.context": "上下文",
    "agent.tools": "工具与审批",
    "agent.skills": "Skills",
    "agent.budget": "预算",
    "agent.session": "会话默认值",
    "gateway": "网关",
    "gateway.websocket": "WebSocket",
    "gateway.limits": "网关限制",
    "gateway.delta": "流式增量",
    "gateway.history": "历史分页",
    "gateway.backpressure": "背压",
    "storage": "存储",
    "storage.checkpoints": "Checkpoint",
    "logging": "日志",
    "ui": "界面",
}

#: Preferred section order for UIs; unknown sections are appended alphabetically.
SECTION_ORDER: tuple[str, ...] = (
    "agent.llm",
    "agent.profiles",
    "agent.context",
    "agent.tools",
    "agent.skills",
    "agent.budget",
    "agent.session",
    "gateway",
    "gateway.websocket",
    "gateway.limits",
    "gateway.delta",
    "gateway.history",
    "gateway.backpressure",
    "storage",
    "storage.checkpoints",
    "logging",
    "ui",
)

#: Per-field overrides.  Anything omitted is inferred from the dataclass.
FIELD_META: dict[str, dict[str, Any]] = {
    # -- agent.llm ------------------------------------------------------
    "agent.llm.provider": {
        "title": "Provider",
        "type": "enum",
        "enum": tuple(sorted(PROVIDER_DEFAULTS)),
        "allow_custom": True,
        "description": "模型厂商；未知厂商需要同时设置 base_url。",
    },
    "agent.llm.model": {
        "title": "模型 ID",
        "description": "厂商使用的模型名，例如 deepseek-chat 或 gpt-4o-mini。",
    },
    "agent.llm.openai_mode": {
        "title": "OpenAI 模式",
        "type": "enum",
        "enum": ("responses", "chat_completions"),
        "nullable": True,
        "description": "仅 OpenAI 系 provider 使用；null 表示沿用厂商默认。",
    },
    "agent.llm.api_key": {
        "title": "API Key",
        "secret": True,
        "description": "可写字面值（文件权限 0600）或 ${ENV_VAR}。",
    },
    "agent.llm.base_url": {
        "title": "Base URL",
        "nullable": True,
        "description": "自定义/兼容端点；null 表示使用厂商默认地址。",
    },
    "agent.llm.timeout": {
        "title": "请求超时（秒）",
        "type": "float",
        "minimum": 1,
    },
    "agent.llm.temperature": {
        "title": "Temperature",
        "type": "float",
        "minimum": 0,
        "maximum": 2,
        "nullable": True,
    },
    "agent.llm.max_tokens": {
        "title": "最大输出 tokens",
        "type": "int",
        "minimum": 1,
    },
    "agent.llm.top_p": {
        "title": "Top P",
        "type": "float",
        "minimum": 0,
        "maximum": 1,
        "nullable": True,
    },
    "agent.llm.reasoning_effort": {
        "title": "Reasoning effort",
        "nullable": True,
        "advanced": True,
    },
    "agent.llm.thinking": {
        "title": "Thinking",
        "type": "bool",
        "nullable": True,
        "advanced": True,
    },
    # -- agent profiles -------------------------------------------------
    "agent.default_profile": {
        "title": "默认 Profile",
        "section": "agent.profiles",
        "description": "未显式指定 profile 的会话使用它；'default' 表示 agent.llm。",
    },
    "agent.profiles": {
        "title": "Profiles",
        "type": "map",
        "item_type": "object",
        "item_section": "agent.llm",
        "section": "agent.profiles",
        "advanced": True,
        "description": "命名模型配置；会话创建时通过 session.create {profile} 选择。",
    },
    "agent.fallbacks": {
        "title": "Fallback 链",
        "type": "list",
        "item_type": "object",
        "item_section": "agent.llm",
        "section": "agent.profiles",
        "advanced": True,
        "description": "选中 profile 失败后按顺序尝试的 provider/model 候选。",
    },
    # -- agent.context --------------------------------------------------
    "agent.context.policy": {
        "title": "上下文策略",
        "type": "enum",
        "enum": ("passthrough", "compacting"),
    },
    "agent.context.max_tokens": {
        "title": "上下文预算 tokens",
        "type": "int",
        "minimum": 0,
    },
    "agent.context.compact_to_tokens": {
        "title": "压缩后目标 tokens",
        "type": "int",
        "minimum": 0,
    },
    "agent.context.keep_last_messages": {
        "title": "保留最近消息数",
        "type": "int",
        "minimum": 1,
    },
    "agent.context.max_summary_tokens": {
        "title": "摘要 tokens 上限",
        "type": "int",
        "minimum": 1,
    },
    # -- agent.tools ----------------------------------------------------
    "agent.tools.approval": {
        "title": "审批策略",
        "type": "enum",
        "enum": ("ask", "allow", "deny"),
        "description": "ask=人工审批；allow=自动放行；deny=自动拒绝。",
    },
    "agent.tools.auto_approve": {
        "title": "免审批工具",
        "type": "list",
        "item_type": "str",
        "description": "approval=ask 时这些工具直接执行；deny 仍会拒绝。",
    },
    "agent.tools.bash_timeout": {
        "title": "Bash 超时（秒）",
        "type": "float",
        "minimum": 0.1,
    },
    # -- agent.skills ---------------------------------------------------
    "agent.skills.enabled": {
        "title": "启用 Skills",
        "type": "bool",
        "description": "关闭后不扫描技能目录、不注册加载工具、不注入索引。",
    },
    "agent.skills.allow_project": {
        "title": "允许项目技能",
        "type": "bool",
        "description": "允许 <workspace>/.linteum/skills；同名时项目级覆盖用户级。",
    },
    "agent.skills.system_dirs": {
        "title": "用户技能目录",
        "type": "list",
        "item_type": "path",
        "description": "默认 linteum_home()/skills，遵循 LINTEUM_HOME。",
    },
    "agent.skills.project_dir": {
        "title": "项目技能目录",
        "description": "相对 workspace 的路径，默认 .linteum/skills。",
    },
    "agent.skills.extra_dirs": {
        "title": "额外技能目录",
        "type": "list",
        "item_type": "path",
        "advanced": True,
    },
    "agent.skills.max_index_bytes": {
        "title": "索引预算字节", "type": "int", "minimum": 512,
    },
    "agent.skills.max_result_bytes": {
        "title": "单次工具结果字节", "type": "int", "minimum": 1, "maximum": 7679,
    },
    "agent.skills.max_skill_bytes": {
        "title": "SKILL.md 上限字节", "type": "int", "minimum": 1,
    },
    "agent.skills.max_resource_bytes": {
        "title": "单资源上限字节", "type": "int", "minimum": 1,
    },
    "agent.skills.max_resources_per_skill": {
        "title": "单技能资源数", "type": "int", "minimum": 1,
    },
    "agent.skills.max_skills": {
        "title": "技能数量上限", "type": "int", "minimum": 1,
    },
    "agent.skills.max_catalog_bytes": {
        "title": "Catalog 内存预算字节", "type": "int", "minimum": 1,
    },
    # -- agent.budget ---------------------------------------------------
    "agent.budget.tokens": {"title": "Token 上限（0=不限）", "type": "int", "minimum": 0},
    "agent.budget.usd": {"title": "费用上限 USD（0=不限）", "type": "float", "minimum": 0},
    "agent.budget.wall_ms": {"title": "时长上限 ms（0=不限）", "type": "int", "minimum": 0},
    "agent.budget.rounds": {"title": "轮数上限（0=不限）", "type": "int", "minimum": 0},
    # -- agent.session --------------------------------------------------
    "agent.session.input_policy": {
        "title": "忙碌输入策略",
        "type": "enum",
        "enum": ("queue", "interrupt", "reject"),
        "description": "agent 忙碌时收到新消息的处理方式。",
    },
    "agent.session.idle_ttl_ms": {
        "title": "空闲回收 ms",
        "type": "int",
        "minimum": 0,
    },
    # -- gateway --------------------------------------------------------
    "gateway.transport": {
        "title": "传输方式",
        "type": "enum",
        "enum": ("auto", "inproc", "stdio", "websocket"),
    },
    "gateway.websocket.host": {
        "title": "监听地址",
        "description": "默认只监听 loopback。",
    },
    "gateway.websocket.port": {
        "title": "监听端口",
        "type": "int",
        "minimum": 1,
        "maximum": 65535,
    },
    "gateway.limits.client_queue_frames": {"title": "客户端队列帧数", "type": "int", "minimum": 1},
    "gateway.limits.client_queue_bytes": {"title": "客户端队列字节", "type": "int", "minimum": 1},
    "gateway.limits.max_command_bytes": {"title": "命令最大字节", "type": "int", "minimum": 1},
    "gateway.limits.max_frame_bytes": {"title": "帧最大字节", "type": "int", "minimum": 1},
    "gateway.delta.coalesce_window_ms": {"title": "增量合并窗口 ms", "type": "int", "minimum": 0},
    "gateway.history.page_size": {"title": "历史每页条数", "type": "int", "minimum": 1},
    "gateway.history.max_page_bytes": {"title": "历史每页字节", "type": "int", "minimum": 1},
    "gateway.backpressure.drop_ephemeral": {"title": "压力时丢弃增量", "type": "bool"},
    "gateway.backpressure.ephemeral_high_watermark": {
        "title": "增量丢弃水位",
        "type": "float",
        "minimum": 0,
        "maximum": 1,
    },
    # -- storage --------------------------------------------------------
    "storage.state_db": {"title": "SQLite 路径", "type": "path"},
    "storage.artifacts": {"title": "Artifact 目录", "type": "path"},
    "storage.checkpoints.keep": {"title": "保留 checkpoint 数", "type": "int", "minimum": 0},
    "storage.checkpoints.every_n_events": {
        "title": "每 N 个事件快照",
        "type": "int",
        "minimum": 0,
    },
    # -- logging --------------------------------------------------------
    "logging.level": {
        "title": "日志级别",
        "type": "enum",
        "enum": ("debug", "info", "warning", "error"),
    },
    "logging.format": {
        "title": "日志格式",
        "type": "enum",
        "enum": ("text", "json"),
    },
    "logging.dir": {"title": "日志目录", "type": "path"},
    "logging.file": {"title": "日志文件名"},
    "logging.path": {"title": "日志完整路径", "type": "path", "nullable": True},
    "logging.include_content": {"title": "记录 payload 内容", "type": "bool"},
    "logging.log_deltas": {"title": "记录增量事件", "type": "bool", "advanced": True},
    "logging.max_bytes": {"title": "日志轮转字节", "type": "int", "minimum": 0},
    "logging.backups": {"title": "日志备份数", "type": "int", "minimum": 0},
    "logging.console": {"title": "同时输出到 stderr", "type": "bool"},
    "logging.max_field_bytes": {"title": "单字段截断字节", "type": "int", "minimum": 0},
    # -- ui -------------------------------------------------------------
    "ui.language": {"title": "界面语言"},
    "ui.theme": {
        "title": "主题",
        "type": "enum",
        "enum": ("auto", "light", "dark"),
    },
    "ui.show_reasoning": {"title": "显示 reasoning", "type": "bool"},
}

_RESTART_PREFIXES: tuple[str, ...] = ("storage.", "gateway.websocket.")
_RESTART_KEYS = frozenset({
    "gateway.transport",
    "logging.dir",
    "logging.file",
    "logging.path",
    "logging.format",
    "logging.max_bytes",
    "logging.backups",
})
_LIVE_PREFIXES: tuple[str, ...] = (
    "gateway.limits.",
    "gateway.delta.",
    "gateway.history.",
    "gateway.backpressure.",
    "ui.",
)
_LIVE_KEYS = frozenset({
    "agent.session.input_policy",
    "agent.session.idle_ttl_ms",
    "logging.level",
    "logging.console",
    "logging.include_content",
    "logging.log_deltas",
})


def apply_scope(key: str) -> str:
    """Return ``live`` / ``new_session`` / ``restart`` for a dotted key."""
    if key in _RESTART_KEYS or key.startswith(_RESTART_PREFIXES):
        return "restart"
    if key in _LIVE_KEYS or key.startswith(_LIVE_PREFIXES):
        return "live"
    return "new_session"


@dataclass(frozen=True, slots=True)
class FieldSpec:
    """One editable leaf setting."""

    key: str
    type: str
    default: Any
    title: str
    description: str = ""
    enum: tuple[str, ...] = ()
    minimum: float | None = None
    maximum: float | None = None
    secret: bool = False
    advanced: bool = False
    apply_scope: str = "new_session"
    nullable: bool = False
    allow_custom: bool = False
    section: str = ""
    item_type: str = ""
    item_section: str = ""

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "type": self.type,
            "default": self.default,
            "title": self.title,
            "description": self.description,
            "enum": list(self.enum),
            "minimum": self.minimum,
            "maximum": self.maximum,
            "secret": self.secret,
            "advanced": self.advanced,
            "apply_scope": self.apply_scope,
            "nullable": self.nullable,
            "allow_custom": self.allow_custom,
            "section": self.section,
            "item_type": self.item_type,
            "item_section": self.item_section,
        }


def _humanize(name: str) -> str:
    return name.replace("_", " ").replace("-", " ").strip().title()


def _infer_type(default: Any) -> str:
    if isinstance(default, bool):
        return "bool"
    if isinstance(default, int):
        return "int"
    if isinstance(default, float):
        return "float"
    if isinstance(default, str):
        return "str"
    return "str"


def _walk(prefix: str, value: Any, out: list[tuple[str, Any]]) -> None:
    for f in fields(value):
        if f.name in ("extra", "version"):
            continue
        key = f"{prefix}.{f.name}" if prefix else f.name
        child = getattr(value, f.name)
        if is_dataclass(child) and not isinstance(child, type):
            _walk(key, child, out)
        else:
            out.append((key, child))


def _make_spec(key: str, default: Any) -> FieldSpec:
    meta = FIELD_META.get(key, {})
    section = meta.get("section") or (key.rsplit(".", 1)[0] if "." in key else "")
    nullable = bool(meta.get("nullable", default is None))
    field_type = meta.get("type") or _infer_type(default)
    return FieldSpec(
        key=key,
        type=field_type,
        default=default,
        title=meta.get("title") or _humanize(key.rsplit(".", 1)[-1]),
        description=meta.get("description", ""),
        enum=tuple(meta.get("enum", ())),
        minimum=meta.get("minimum"),
        maximum=meta.get("maximum"),
        secret=bool(meta.get("secret", False)),
        advanced=bool(meta.get("advanced", False)),
        apply_scope=apply_scope(key),
        nullable=nullable,
        allow_custom=bool(meta.get("allow_custom", False)),
        section=section,
        item_type=meta.get("item_type", ""),
        item_section=meta.get("item_section", ""),
    )


def field_specs(config: AppConfig | None = None) -> list[FieldSpec]:
    """Return every editable leaf field, in schema walk order."""
    config = config if config is not None else default_config()
    pairs: list[tuple[str, Any]] = []
    _walk("", config, pairs)
    return [_make_spec(key, default) for key, default in pairs]


def build_schema(config: AppConfig | None = None) -> dict:
    """Return the schema document intended for CLI/TUI/Web forms."""
    specs = field_specs(config)
    by_section: dict[str, list[FieldSpec]] = {}
    for spec in specs:
        by_section.setdefault(spec.section, []).append(spec)

    order = list(SECTION_ORDER) + sorted(
        key for key in by_section if key not in SECTION_ORDER
    )
    sections = []
    for key in order:
        items = by_section.get(key)
        if not items:
            continue
        sections.append({
            "key": key,
            "title": SECTION_TITLES.get(key, _humanize(key.replace(".", " "))),
            "fields": [item.to_dict() for item in items],
        })
    return {"version": CONFIG_VERSION, "sections": sections}


def _dynamic_spec(key: str, config: AppConfig) -> FieldSpec | None:
    """Resolve ``agent.profiles.<name>.<field>`` / ``agent.fallbacks.<i>.<field>``."""
    for prefix in ("agent.profiles.", "agent.fallbacks."):
        if not key.startswith(prefix):
            continue
        parts = key[len(prefix):].split(".")
        if len(parts) == 1:
            if prefix == "agent.profiles.":
                return FieldSpec(
                    key=key, type="object", default={},
                    title=f"Profile {parts[0]}",
                    description="A named LLM profile.",
                    apply_scope="new_session", section="agent.profiles",
                    item_type="object", item_section="agent.llm",
                )
            if prefix == "agent.fallbacks." and parts[0].isdigit():
                return FieldSpec(
                    key=key, type="object", default={},
                    title=f"Fallback {parts[0]}",
                    description="An LLM fallback candidate.",
                    apply_scope="new_session", section="agent.profiles",
                    item_type="object", item_section="agent.llm",
                )
            return None
        name = ".".join(parts[1:])
        if name not in {f.name for f in fields(LLMConfig)}:
            return None
        base = FIELD_META.get(f"agent.llm.{name}", {})
        sample = LLMConfig(provider="openai", model="gpt-4o-mini")
        default = getattr(sample, name, None)
        return FieldSpec(
            key=key,
            type=base.get("type") or _infer_type(default),
            default=default,
            title=base.get("title") or _humanize(name),
            description=base.get("description", ""),
            enum=tuple(base.get("enum", ())),
            minimum=base.get("minimum"),
            maximum=base.get("maximum"),
            secret=bool(base.get("secret", False)),
            advanced=bool(base.get("advanced", False)),
            apply_scope="new_session",
            nullable=bool(base.get("nullable", default is None)),
            allow_custom=bool(base.get("allow_custom", False)),
            section="agent.profiles",
        )
    return None


def get_spec(key: str, config: AppConfig | None = None) -> FieldSpec:
    config = config if config is not None else default_config()
    for spec in field_specs(config):
        if spec.key == key:
            return spec
    dynamic = _dynamic_spec(key, config)
    if dynamic is not None:
        return dynamic
    raise ConfigError(f"unknown config key: {key}", key=key, code="E_CONFIG_UNKNOWN")


def coerce_value(spec: FieldSpec, value: Any) -> Any:
    """Coerce a CLI/env string into the field's declared type."""
    if value is None:
        if spec.nullable:
            return None
        raise ConfigError(f"{spec.key} must not be null", key=spec.key,
                          code="E_CONFIG_TYPE")

    base = spec.type
    if base == "enum":
        text = str(value)
        if spec.nullable and text.strip().lower() in ("", "none", "null"):
            return None
        if text not in spec.enum:
            if spec.allow_custom:
                return text
            raise ConfigError(
                f"{spec.key} must be one of {', '.join(spec.enum)}; got {text!r}",
                key=spec.key, code="E_CONFIG_ENUM",
            )
        return text
    if base in ("map", "list", "object"):
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError as exc:
                raise ConfigError(
                    f"{spec.key} must be valid JSON: {exc}",
                    key=spec.key, code="E_CONFIG_JSON",
                ) from exc
        if base in ("map", "object"):
            if not isinstance(value, dict):
                raise ConfigError(f"{spec.key} must be a JSON object",
                                  key=spec.key, code="E_CONFIG_TYPE")
            return value
        if not isinstance(value, (list, tuple)):
            raise ConfigError(f"{spec.key} must be a JSON array",
                              key=spec.key, code="E_CONFIG_TYPE")
        return list(value)
    if base == "path":
        base = "str"
    if base == "secret":
        base = "str"

    kind = base
    if spec.nullable:
        kind = f"optional_{base}"
    result = _coerce(value, kind, spec.key)

    if spec.minimum is not None and result is not None and result < spec.minimum:
        raise ConfigError(f"{spec.key} must be >= {spec.minimum}", key=spec.key,
                          code="E_CONFIG_RANGE")
    if spec.maximum is not None and result is not None and result > spec.maximum:
        raise ConfigError(f"{spec.key} must be <= {spec.maximum}", key=spec.key,
                          code="E_CONFIG_RANGE")
    return result


__all__ = [
    "FIELD_META",
    "SECTION_ORDER",
    "SECTION_TITLES",
    "FieldSpec",
    "apply_scope",
    "build_schema",
    "coerce_value",
    "field_specs",
    "get_spec",
]
