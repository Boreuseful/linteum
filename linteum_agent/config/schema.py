"""Config data model: the LLM config (OpenAI / Anthropic / OpenAI-compatible endpoints, etc.).

The YAML structure of the config file::

    version: 1
    # The LLM used by default (provider and model are semantically required; other fields may be omitted).
    llm:
      provider: openai            # openai / openai_compatible / anthropic / deepseek ...
      model: gpt-4o-mini
      openai_mode: responses      # responses / chat_completions
      api_key: sk-xxx
      base_url: null              # custom gateway or compatible-endpoint address
      timeout: 60.0
      temperature: 0.7
      max_tokens: 1024
      top_p: null
      reasoning_effort: null
      thinking: null

``Config`` (this module) handles the data and (de)serialization; file read/write is in
``linteum_agent.config.store``.

Design conventions:

- **Single LLM entry**: the config file has only one ``llm`` section; any LLM config must first be
  normalized to ``LLMConfig`` (``llm_from_dict`` + ``validate_llm_config``) before it can be loaded.
  The ``llms`` style of "multiple configs fetched by name" is not supported; that key raises an error.
- The LLM config directly reuses ``model.config.LLMConfig`` without redefining fields; serialization
  is driven by ``dataclasses.fields()``, so new ``LLMConfig`` fields automatically appear in the YAML.
- ``api_key`` / ``base_url`` may be ``None``: the provider then falls back to environment variables
  (``OPENAI_API_KEY`` / ``DEEPSEEK_API_KEY`` / ``ANTHROPIC_API_KEY``, etc.).
- Unknown LLM fields raise ``ConfigError`` to avoid silently ignoring typos; unrecognized **top-level**
  keys are collected into ``Config.extra`` and preserved as-is (read-write round trips lose nothing).
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from typing import Any, Mapping

try:  # intra-package import
    from ..model.config import LLMConfig
except ImportError:  # supports adding /workspace/linteum_agent to sys.path directly
    from model.config import LLMConfig


# Config file version; increment on incompatible field-structure changes.
CONFIG_VERSION = 1

# Top-level key names.
VERSION_KEY = "version"
LLM_KEY = "llm"

# Removed top-level keys: their presence gives a clear migration error instead of being silently ignored as extra.
_REMOVED_TOP_LEVEL_KEYS = frozenset({"llms"})

# Default values used when provider / model are not explicitly configured.
DEFAULT_PROVIDER = "openai"
DEFAULT_MODEL = "gpt-4o-mini"

# Kept consistent with the providers supported by ``model.client.LLMClient``.
SUPPORTED_PROVIDERS = frozenset(
    {
        "openai",
        "openai_compatible",
        "anthropic",
        "deepseek",
        "deepseek_compatible",
    }
)

# Kept consistent with ``model.config.OpenAIMode``.
SUPPORTED_OPENAI_MODES = frozenset({"responses", "chat_completions"})

# Reserved top-level keys; other keys go into ``Config.extra``.
_RESERVED_TOP_LEVEL = frozenset({VERSION_KEY, LLM_KEY})

# LLM field aliases: support common camelCase / kebab-case forms (underscores are stripped before comparison).
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

# All field names of ``LLMConfig`` (including provider / model).
_LLM_FIELD_NAMES = frozenset(item.name for item in fields(LLMConfig))
# Fields to be handled as "optional string".
_LLM_OPTIONAL_STR_FIELDS = frozenset({"api_key", "base_url", "reasoning_effort"})
# Fields to be handled as "float".
_LLM_FLOAT_FIELDS = frozenset({"timeout", "temperature", "top_p"})

_TRUE_VALUES = frozenset({"true", "yes", "y", "1", "on"})
_FALSE_VALUES = frozenset({"false", "no", "n", "0", "off", "none", "null", ""})


class ConfigError(ValueError):
    """The config file content is invalid (format error, unknown field, illegal value, etc.)."""


# ----------------------------------------------------------------------
# Value normalization
# ----------------------------------------------------------------------


def _to_str(value: Any, name: str) -> str:
    """Normalize to a non-empty string; raise ``ConfigError`` on empty or clearly invalid values."""
    if value is None or (isinstance(value, str) and not value.strip()):
        raise ConfigError(f"config field {name!r} must not be empty")
    return value if isinstance(value, str) else str(value)


def _to_optional_str(value: Any) -> str | None:
    """Normalize to an optional string; ``None`` / an empty string is treated as unconfigured."""
    if value is None:
        return None
    text = value if isinstance(value, str) else str(value)
    return text if text.strip() else None


def _to_float(value: Any, name: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"config field {name!r} must be a number, got {value!r}") from exc


def _to_int(value: Any, name: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"config field {name!r} must be an integer, got {value!r}") from exc


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
    raise ConfigError(f"config field {name!r} must be a boolean, got {value!r}")


def _coerce_llm_field(name: str, value: Any) -> Any:
    """Normalize a YAML value into a Python value per the ``LLMConfig`` field semantics."""
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
    """Return the default LLM config (OpenAI Responses API + ``gpt-4o-mini``)."""
    return LLMConfig(provider=DEFAULT_PROVIDER, model=DEFAULT_MODEL)


def validate_llm_config(config: LLMConfig) -> LLMConfig:
    """Validate the LLM config and return the object; raise ``ConfigError`` when invalid."""
    if config.provider not in SUPPORTED_PROVIDERS:
        raise ConfigError(
            f"unsupported provider: {config.provider!r}; "
            f"available: {', '.join(sorted(SUPPORTED_PROVIDERS))}"
        )
    if not str(config.model or "").strip():
        raise ConfigError("llm.model must not be empty")
    if config.openai_mode not in SUPPORTED_OPENAI_MODES:
        raise ConfigError(
            f"unsupported openai_mode: {config.openai_mode!r}; "
            f"available: {', '.join(sorted(SUPPORTED_OPENAI_MODES))}"
        )
    if config.timeout is not None and float(config.timeout) <= 0:
        raise ConfigError(f"llm.timeout must be greater than 0, got {config.timeout!r}")
    if config.max_tokens is not None and int(config.max_tokens) <= 0:
        raise ConfigError(f"llm.max_tokens must be greater than 0, got {config.max_tokens!r}")
    if not config.base_url and config.provider == "openai_compatible":
        raise ConfigError("base_url is required when provider='openai_compatible'")
    return config


def llm_to_dict(config: LLMConfig) -> dict[str, Any]:
    """Convert ``LLMConfig`` into a YAML-writable dict (field order matches the dataclass)."""
    return {item.name: getattr(config, item.name) for item in fields(config)}


def llm_from_dict(data: Mapping[str, Any] | None) -> LLMConfig:
    """Build ``LLMConfig`` from a dict; unknown fields / invalid values raise ``ConfigError``.

    ``provider`` defaults to ``openai`` and ``model`` to ``gpt-4o-mini``, so a named config can write
    only the fields it needs to override.
    """
    if data is None:
        return default_llm_config()
    if not isinstance(data, Mapping):
        raise ConfigError(f"llm config must be a mapping, got {type(data).__name__}")

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
            f"llm config has unknown field(s): {', '.join(sorted(unknown))}; "
            f"available fields: {', '.join(sorted(_LLM_FIELD_NAMES))}"
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
    """Project config: a single LLM (``LLMConfig``) + unrecognized top-level keys + version.

    There is only one LLM config in the file (``llm``): any LLM config must first be normalized to
    ``LLMConfig`` before it can be loaded (see ``llm_from_dict`` / ``validate_llm_config``). Multiple
    configs are managed by the caller constructing separate ``LLMConfig`` objects in code and are not
    written into the config file. ``extra`` stores unrecognized top-level keys so read-write round
    trips lose nothing.
    """

    llm: LLMConfig = field(default_factory=default_llm_config)
    extra: dict[str, Any] = field(default_factory=dict)
    version: int = CONFIG_VERSION

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    @classmethod
    def default(cls) -> "Config":
        """Return the default config (a blank config not yet written to disk)."""
        return cls()

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> "Config":
        """Build a config from the dict parsed out of YAML."""
        if data is None:
            return cls.default()
        if not isinstance(data, Mapping):
            raise ConfigError(f"config file top level must be a mapping, got {type(data).__name__}")

        version = data.get(VERSION_KEY, CONFIG_VERSION)
        try:
            version = int(version)
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"version must be an integer, got {version!r}") from exc
        if version > CONFIG_VERSION:
            raise ConfigError(
                f"config file version={version} is higher than the currently supported {CONFIG_VERSION}; please upgrade linteum_agent"
            )

        removed = sorted(_REMOVED_TOP_LEVEL_KEYS & set(data))
        if removed:
            raise ConfigError(
                f"config no longer supports top-level key(s) {', '.join(removed)}: the LLM config must be "
                "normalized to LLMConfig and written under 'llm' (for multiple configs in code, construct "
                "LLMConfig directly)"
            )

        llm_raw = data.get(LLM_KEY)
        if llm_raw is not None and not isinstance(llm_raw, Mapping):
            raise ConfigError(f"llm config must be a mapping, got {type(llm_raw).__name__}")
        llm = llm_from_dict(llm_raw if isinstance(llm_raw, Mapping) else None)

        extra = {key: value for key, value in data.items() if key not in _RESERVED_TOP_LEVEL}

        return cls(
            llm=llm,
            extra=extra,
            version=version,
        )

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """Convert to a YAML-writable dict; ``extra`` is appended at the end."""
        data: dict[str, Any] = {
            VERSION_KEY: self.version,
            LLM_KEY: llm_to_dict(self.llm),
        }
        data.update(self.extra)
        return data

    # ------------------------------------------------------------------
    # LLM read/write
    # ------------------------------------------------------------------

    def set_llm(self, config: LLMConfig) -> "Config":
        """Write the LLM config; it must pass ``validate_llm_config`` first (only normalized configs are loaded)."""
        self.llm = validate_llm_config(config)
        return self


def empty_config() -> Config:
    """Return a blank config, equivalent to ``Config.default()``."""
    return Config.default()

