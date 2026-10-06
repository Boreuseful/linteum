"""配置数据模型：LLM 配置（OpenAI / Anthropic / OpenAI 兼容接口等）。

配置文件的 YAML 结构::

    version: 1
    # 默认使用的 LLM（provider 与 model 为必填语义，其余字段可省略）。
    llm:
      provider: openai            # openai / openai_compatible / anthropic / deepseek ...
      model: gpt-4o-mini
      openai_mode: responses      # responses / chat_completions
      api_key: sk-xxx
      base_url: null              # 自定义网关或兼容接口地址
      timeout: 60.0
      temperature: 0.7
      max_tokens: 1024
      top_p: null
      reasoning_effort: null
      thinking: null

``Config``（本模块）负责数据与 (反)序列化，文件读写见 ``linteum_agent.config.store``。

设计约定：

- **单一 LLM 入口**：配置文件里只有 ``llm`` 一处 LLM 配置；任何 LLM 配置都必须先
  规范化为 ``LLMConfig``（``llm_from_dict`` + ``validate_llm_config``）才能被加载。
  不支持 ``llms`` 这类「多套配置按名字取」的写法，出现该键会直接报错。
- LLM 配置直接复用 ``model.config.LLMConfig``，不重复定义字段；序列化由
  ``dataclasses.fields()`` 驱动，``LLMConfig`` 新增字段会自动出现在 YAML 中。
- ``api_key`` / ``base_url`` 允许为 ``None``：此时由 provider 回退到环境变量
  （``OPENAI_API_KEY`` / ``DEEPSEEK_API_KEY`` / ``ANTHROPIC_API_KEY`` 等）。
- 未知的 LLM 字段会抛 ``ConfigError``，避免拼写错误被静默忽略；未识别的**顶层**
  键会被收进 ``Config.extra`` 原样保留（读取-写回不丢内容）。
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from typing import Any, Mapping

try:  # 包内导入
    from ..model.config import LLMConfig
except ImportError:  # 支持直接将 /workspace/linteum_agent 加入 sys.path
    from model.config import LLMConfig


# 配置文件版本；字段结构不兼容变更时递增。
CONFIG_VERSION = 1

# 顶层键名。
VERSION_KEY = "version"
LLM_KEY = "llm"

# 已移除的顶层键：出现时给出明确的迁移错误，避免被当作 extra 静默忽略。
_REMOVED_TOP_LEVEL_KEYS = frozenset({"llms"})

# 未显式配置 provider / model 时使用的默认值。
DEFAULT_PROVIDER = "openai"
DEFAULT_MODEL = "gpt-4o-mini"

# 与 ``model.client.LLMClient`` 支持的 provider 保持一致。
SUPPORTED_PROVIDERS = frozenset(
    {
        "openai",
        "openai_compatible",
        "anthropic",
        "deepseek",
        "deepseek_compatible",
    }
)

# 与 ``model.config.OpenAIMode`` 保持一致。
SUPPORTED_OPENAI_MODES = frozenset({"responses", "chat_completions"})

# 顶层保留键：其余键会进入 ``Config.extra``。
_RESERVED_TOP_LEVEL = frozenset({VERSION_KEY, LLM_KEY})

# LLM 字段别名：兼容 camelCase / kebab-case 等常见写法（比较前会去掉下划线）。
_LLM_ALIASES = {
    "apikey": "api_key",
    "api-key": "api_key",
    "baseurl": "base_url",
    "base-url": "base_url",
    "openaimode": "openai_mode",
    "openai-mode": "openai_mode",
    "maxtokens": "max_tokens",
    "max-tokens": "max_tokens",
    "topp": "top_p",
    "top-p": "top_p",
    "reasoningeffort": "reasoning_effort",
    "reasoning-effort": "reasoning_effort",
}

# ``LLMConfig`` 的全部字段名（含 provider / model）。
_LLM_FIELD_NAMES = frozenset(item.name for item in fields(LLMConfig))
# 需要按“可选字符串”处理的字段。
_LLM_OPTIONAL_STR_FIELDS = frozenset({"api_key", "base_url", "reasoning_effort"})
# 需要按“浮点数”处理的字段。
_LLM_FLOAT_FIELDS = frozenset({"timeout", "temperature", "top_p"})

_TRUE_VALUES = frozenset({"true", "yes", "y", "1", "on"})
_FALSE_VALUES = frozenset({"false", "no", "n", "0", "off", "none", "null", ""})


class ConfigError(ValueError):
    """配置文件内容不合法（格式错误、字段未知、取值非法等）。"""


# ----------------------------------------------------------------------
# 取值归一化
# ----------------------------------------------------------------------


def _to_str(value: Any, name: str) -> str:
    """归一化为非空字符串；空值或类型明显不合法时抛 ``ConfigError``。"""
    if value is None or (isinstance(value, str) and not value.strip()):
        raise ConfigError(f"配置字段 {name!r} 不能为空")
    return value if isinstance(value, str) else str(value)


def _to_optional_str(value: Any) -> str | None:
    """归一化为可选字符串；``None`` / 空串视为未配置。"""
    if value is None:
        return None
    text = value if isinstance(value, str) else str(value)
    return text if text.strip() else None


def _to_float(value: Any, name: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"配置字段 {name!r} 需要数字，实际为 {value!r}") from exc


def _to_int(value: Any, name: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"配置字段 {name!r} 需要整数，实际为 {value!r}") from exc


def _to_optional_bool(value: Any, name: str) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in _TRUE_VALUES:
        return True
    if text in _FALSE_VALUES:
        return False
    raise ConfigError(f"配置字段 {name!r} 需要布尔值，实际为 {value!r}")


def _coerce_llm_field(name: str, value: Any) -> Any:
    """按 ``LLMConfig`` 字段语义把 YAML 值归一化为 Python 值。"""
    if value is None:
        return None
    if name in _LLM_OPTIONAL_STR_FIELDS:
        return _to_optional_str(value)
    if name == "openai_mode":
        return _to_str(value, name)
    if name in _LLM_FLOAT_FIELDS:
        return _to_float(value, name)
    if name == "max_tokens":
        return _to_int(value, name)
    if name == "thinking":
        return _to_optional_bool(value, name)
    return value


# ----------------------------------------------------------------------
# LLMConfig <-> dict
# ----------------------------------------------------------------------


def default_llm_config() -> LLMConfig:
    """返回默认 LLM 配置（OpenAI Responses API + ``gpt-4o-mini``）。"""
    return LLMConfig(provider=DEFAULT_PROVIDER, model=DEFAULT_MODEL)


def validate_llm_config(config: LLMConfig) -> LLMConfig:
    """校验 LLM 配置并返回原对象；不合法时抛 ``ConfigError``。"""
    if config.provider not in SUPPORTED_PROVIDERS:
        raise ConfigError(
            f"不支持的 provider: {config.provider!r}；"
            f"可选值: {', '.join(sorted(SUPPORTED_PROVIDERS))}"
        )
    if not str(config.model or "").strip():
        raise ConfigError("llm.model 不能为空")
    if config.openai_mode not in SUPPORTED_OPENAI_MODES:
        raise ConfigError(
            f"不支持的 openai_mode: {config.openai_mode!r}；"
            f"可选值: {', '.join(sorted(SUPPORTED_OPENAI_MODES))}"
        )
    if config.timeout is not None and float(config.timeout) <= 0:
        raise ConfigError(f"llm.timeout 必须大于 0，实际为 {config.timeout!r}")
    if config.max_tokens is not None and int(config.max_tokens) <= 0:
        raise ConfigError(f"llm.max_tokens 必须大于 0，实际为 {config.max_tokens!r}")
    if not config.base_url and config.provider == "openai_compatible":
        raise ConfigError("provider='openai_compatible' 时必须配置 base_url")
    return config


def llm_to_dict(config: LLMConfig) -> dict[str, Any]:
    """把 ``LLMConfig`` 转成可写 YAML 的 dict（字段顺序与 dataclass 一致）。"""
    return {item.name: getattr(config, item.name) for item in fields(config)}


def llm_from_dict(data: Mapping[str, Any] | None) -> LLMConfig:
    """从 dict 构造 ``LLMConfig``；未知字段 / 非法取值抛 ``ConfigError``。

    ``provider`` 缺省为 ``openai``，``model`` 缺省为 ``gpt-4o-mini``，
    因此具名配置可以只写需要覆盖的字段。
    """
    if data is None:
        return default_llm_config()
    if not isinstance(data, Mapping):
        raise ConfigError(f"llm 配置需要 mapping，实际为 {type(data).__name__}")

    values: dict[str, Any] = {}
    unknown: list[str] = []
    for raw_key, raw_value in data.items():
        key = str(raw_key)
        name = _LLM_ALIASES.get(key.replace("_", "").lower(), key)
        if name not in _LLM_FIELD_NAMES:
            unknown.append(key)
            continue
        if name == "provider":
            values["provider"] = _to_str(raw_value, "provider")
        elif name == "model":
            values["model"] = _to_str(raw_value, "model")
        else:
            values[name] = _coerce_llm_field(name, raw_value)

    if unknown:
        raise ConfigError(
            f"llm 配置存在未知字段: {', '.join(sorted(unknown))}；"
            f"可用字段: {', '.join(sorted(_LLM_FIELD_NAMES))}"
        )

    config = LLMConfig(
        provider=values.pop("provider", DEFAULT_PROVIDER),
        model=values.pop("model", DEFAULT_MODEL),
        **values,
    )
    return validate_llm_config(config)


# ----------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------


@dataclass
class Config:
    """项目配置：单个 LLM（``LLMConfig``）+ 未识别顶层键 + 版本。

    配置里只有一处 LLM 配置（``llm``）：任何 LLM 配置都必须先规范化为
    ``LLMConfig`` 才能被加载（见 ``llm_from_dict`` / ``validate_llm_config``）。
    多套配置由调用方在代码里各自构造 ``LLMConfig`` 管理，不写进配置文件。
    ``extra`` 保存未识别的顶层键，保证读取-写回不丢内容。
    """

    llm: LLMConfig = field(default_factory=default_llm_config)
    extra: dict[str, Any] = field(default_factory=dict)
    version: int = CONFIG_VERSION

    # ------------------------------------------------------------------
    # 构造
    # ------------------------------------------------------------------

    @classmethod
    def default(cls) -> "Config":
        """返回默认配置（尚未落盘的空白配置）。"""
        return cls()

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> "Config":
        """从 YAML 解析出的 dict 构造配置。"""
        if data is None:
            return cls.default()
        if not isinstance(data, Mapping):
            raise ConfigError(f"配置文件顶层需要 mapping，实际为 {type(data).__name__}")

        version = data.get(VERSION_KEY, CONFIG_VERSION)
        try:
            version = int(version)
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"version 需要整数，实际为 {version!r}") from exc
        if version > CONFIG_VERSION:
            raise ConfigError(
                f"配置文件 version={version} 高于当前支持版本 {CONFIG_VERSION}，请升级 linteum_agent"
            )

        removed = sorted(_REMOVED_TOP_LEVEL_KEYS & set(data))
        if removed:
            raise ConfigError(
                f"配置不再支持顶层键 {', '.join(removed)}：LLM 配置必须规范化为 "
                "LLMConfig 后写入 'llm'（代码侧多套配置请直接构造 LLMConfig）"
            )

        llm_raw = data.get(LLM_KEY)
        if llm_raw is not None and not isinstance(llm_raw, Mapping):
            raise ConfigError(f"llm 配置需要 mapping，实际为 {type(llm_raw).__name__}")
        llm = llm_from_dict(llm_raw if isinstance(llm_raw, Mapping) else None)

        extra = {key: value for key, value in data.items() if key not in _RESERVED_TOP_LEVEL}

        return cls(
            llm=llm,
            extra=extra,
            version=version,
        )

    # ------------------------------------------------------------------
    # 序列化
    # ------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """转成可写 YAML 的 dict；``extra`` 追加在末尾。"""
        data: dict[str, Any] = {
            VERSION_KEY: self.version,
            LLM_KEY: llm_to_dict(self.llm),
        }
        data.update(self.extra)
        return data

    # ------------------------------------------------------------------
    # LLM 读写
    # ------------------------------------------------------------------

    def set_llm(self, config: LLMConfig) -> "Config":
        """写入 LLM 配置；写前必须通过 ``validate_llm_config``（规范后才加载）。"""
        self.llm = validate_llm_config(config)
        return self


def empty_config() -> Config:
    """返回空白配置，等价于 ``Config.default()``。"""
    return Config.default()

