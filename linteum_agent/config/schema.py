"""Configuration data model for ``linteum_agent``.

The config file is deliberately **semi-permanent**: it stores the LLM endpoint
and credentials, gateway limits, storage paths, logging and UI preferences.
Per-conversation values — most importantly ``workspace`` — are *not* part of
this model.  They are supplied through
:class:`~linteum_agent.config.options.SessionOptions` when a session is created
(``session.create {workspace}``), because every conversation may use a
different directory.

The persisted YAML is versioned with :data:`CONFIG_VERSION`.  This is a fresh
configuration layer: it does not read or migrate the legacy ``~/.agent`` file.

Design reference: ``test/new_event_dirvent/tmp_design_doc/config-design-v0.1.md``.
"""

from __future__ import annotations

import math
import os
import re
from collections.abc import Mapping as ABCMapping
from dataclasses import MISSING, dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any, Mapping, Optional, get_args, get_origin, get_type_hints

from ..runtime.llm.config import (
    LLMConfig,
    PROVIDER_DEFAULTS,
    validate_config as validate_llm_config,
)
from ..runtime.observability.config import LogConfig
from .paths import AGENT_DIRNAME, linteum_home

#: Current configuration schema version.  Bumped only for incompatible changes.
CONFIG_VERSION = 1

#: Default display path for the user state directory.
DEFAULT_HOME = f"~/{AGENT_DIRNAME}"

#: Default provider/model used when a config file is created.
DEFAULT_PROVIDER = "openai"
DEFAULT_MODEL = "gpt-4o-mini"

_VALUE_TRUE = frozenset({"1", "true", "yes", "on", "y"})
_VALUE_FALSE = frozenset({"0", "false", "no", "off", "n"})
_VALUE_NONE = frozenset({"", "none", "null"})

#: Explicit scalar kinds for fields whose default is ``None`` or missing.
#: ``(dataclass name, field name) -> kind``.
_FIELD_KINDS: dict[tuple[str, str], str] = {
    ("LLMConfig", "provider"): "str",
    ("LLMConfig", "model"): "str",
    ("LLMConfig", "openai_mode"): "optional_str",
    ("LLMConfig", "api_key"): "optional_str",
    ("LLMConfig", "base_url"): "optional_str",
    ("LLMConfig", "temperature"): "optional_float",
    ("LLMConfig", "top_p"): "optional_float",
    ("LLMConfig", "reasoning_effort"): "optional_str",
    ("LLMConfig", "thinking"): "optional_bool",
    ("LogConfig", "path"): "optional_str",
}


class ConfigError(ValueError):
    """Invalid config content (parse error, unknown key, bad value, conflict)."""

    def __init__(
        self,
        message: str,
        *,
        key: Optional[str] = None,
        code: str = "E_CONFIG",
        hint: str = "",
    ) -> None:
        super().__init__(message)
        self.key = key
        self.code = code
        self.hint = hint

    def to_dict(self) -> dict:
        return {"code": self.code, "key": self.key, "message": str(self), "hint": self.hint}


@dataclass(frozen=True, slots=True)
class Issue:
    """One validation finding (error or warning)."""

    key: str
    code: str
    message: str
    severity: str = "error"
    hint: str = ""

    @property
    def is_error(self) -> bool:
        return self.severity == "error"

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "code": self.code,
            "message": self.message,
            "severity": self.severity,
            "hint": self.hint,
        }


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ContextSettings:
    """Context budget / compaction policy.

    ``policy="passthrough"`` disables compaction (``ContextPolicy(max_tokens=0)``);
    ``policy="compacting"`` maps the remaining fields to a
    :class:`linteum_agent.runtime.context.ContextPolicy`.
    """

    policy: str = "passthrough"          # passthrough | compacting
    max_tokens: int = 120_000
    compact_to_tokens: int = 80_000
    keep_last_messages: int = 8
    max_summary_tokens: int = 4_000


@dataclass(frozen=True, slots=True)
class ToolSettings:
    """Tool execution policy.  ``workspace`` is intentionally absent."""

    approval: str = "ask"                # ask | allow | deny
    bash_timeout: float = 30.0


@dataclass(frozen=True, slots=True)
class SkillsSettings:
    """Third-party skill discovery and progressive-disclosure limits.

    ``system_dirs`` is the user-level root (resolved through ``linteum_home()``
    so ``LINTEUM_HOME`` is honoured); ``project_dir`` is relative to the
    session workspace; ``extra_dirs`` are host-configured trusted roots.
    """

    enabled: bool = True
    allow_project: bool = True
    system_dirs: tuple[str, ...] = field(
        default_factory=lambda: (str(linteum_home() / "skills"),)
    )
    project_dir: str = ".linteum/skills"
    extra_dirs: tuple[str, ...] = ()
    max_index_bytes: int = 8 * 1024
    max_result_bytes: int = 7 * 1024
    max_skill_bytes: int = 256 * 1024
    max_resource_bytes: int = 1024 * 1024
    max_resources_per_skill: int = 64
    max_skills: int = 256
    max_catalog_bytes: int = 16 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class BudgetSettings:
    """Session budget limits (0 = unlimited)."""

    tokens: int = 0
    usd: float = 0.0
    wall_ms: int = 0
    rounds: int = 0


@dataclass(frozen=True, slots=True)
class SessionSettings:
    """Defaults for newly created sessions."""

    input_policy: str = "queue"          # queue | interrupt | reject
    idle_ttl_ms: int = 1_800_000


@dataclass(frozen=True, slots=True)
class AgentSettings:
    """Everything that shapes an Agent, except the per-session workspace.

    ``llm`` is the default profile.  Additional named profiles live in
    ``profiles``; a session picks one by name (``SessionOptions.profile``).
    ``fallbacks`` is an ordered list of extra provider/model candidates tried
    after the selected profile fails, using the runtime router's alias chain.

    One adapter is registered per provider, so profiles sharing a provider must
    agree on ``base_url`` / ``api_key`` / ``openai_mode``.  Use distinct
    provider names when those differ (see validation).
    """

    llm: LLMConfig = field(
        default_factory=lambda: LLMConfig(provider=DEFAULT_PROVIDER, model=DEFAULT_MODEL)
    )
    default_profile: str = "default"
    profiles: dict[str, LLMConfig] = field(default_factory=dict)
    fallbacks: tuple[LLMConfig, ...] = ()
    context: ContextSettings = field(default_factory=ContextSettings)
    tools: ToolSettings = field(default_factory=ToolSettings)
    skills: SkillsSettings = field(default_factory=SkillsSettings)
    budget: BudgetSettings = field(default_factory=BudgetSettings)
    session: SessionSettings = field(default_factory=SessionSettings)

    def profile(self, name: Optional[str] = None) -> LLMConfig:
        """Resolve a profile name; ``"default"`` maps to :attr:`llm`."""
        key = name or self.default_profile
        if key == "default":
            return self.llm
        try:
            return self.profiles[key]
        except KeyError as exc:
            raise ConfigError(
                f"unknown model profile {key!r}",
                key="agent.profiles",
                code="E_CONFIG_PROFILE_NOT_FOUND",
                hint=f"available profiles: {', '.join(self.profile_names())}",
            ) from exc

    def profile_names(self) -> tuple[str, ...]:
        return ("default", *sorted(self.profiles))

    def route_chain(self, name: Optional[str] = None) -> tuple[LLMConfig, ...]:
        """The selected profile followed by the global fallbacks."""
        return (self.profile(name), *self.fallbacks)


@dataclass(frozen=True, slots=True)
class WebSocketSettings:
    host: str = "127.0.0.1"
    port: int = 8765


@dataclass(frozen=True, slots=True)
class GatewayLimits:
    client_queue_frames: int = 1_024
    client_queue_bytes: int = 1_048_576
    max_command_bytes: int = 262_144
    max_frame_bytes: int = 1_048_576


@dataclass(frozen=True, slots=True)
class DeltaSettings:
    coalesce_window_ms: int = 50


@dataclass(frozen=True, slots=True)
class HistorySettings:
    page_size: int = 100
    max_page_bytes: int = 1_048_576


@dataclass(frozen=True, slots=True)
class BackpressureSettings:
    drop_ephemeral: bool = True
    ephemeral_high_watermark: float = 0.75


@dataclass(frozen=True, slots=True)
class GatewaySettings:
    transport: str = "auto"              # auto | inproc | stdio | websocket
    websocket: WebSocketSettings = field(default_factory=WebSocketSettings)
    limits: GatewayLimits = field(default_factory=GatewayLimits)
    delta: DeltaSettings = field(default_factory=DeltaSettings)
    history: HistorySettings = field(default_factory=HistorySettings)
    backpressure: BackpressureSettings = field(default_factory=BackpressureSettings)


@dataclass(frozen=True, slots=True)
class CheckpointSettings:
    keep: int = 3
    every_n_events: int = 500


@dataclass(frozen=True, slots=True)
class StorageSettings:
    state_db: str = field(
        default_factory=lambda: str(linteum_home() / "state.db")
    )
    artifacts: str = field(
        default_factory=lambda: str(linteum_home() / "artifacts")
    )
    checkpoints: CheckpointSettings = field(default_factory=CheckpointSettings)


@dataclass(frozen=True, slots=True)
class UiSettings:
    language: str = "zh-CN"
    theme: str = "auto"                  # auto | light | dark
    show_reasoning: bool = True


@dataclass(frozen=True, slots=True)
class AppConfig:
    """The persisted, semi-permanent configuration."""

    version: int = CONFIG_VERSION
    agent: AgentSettings = field(default_factory=AgentSettings)
    gateway: GatewaySettings = field(default_factory=GatewaySettings)
    storage: StorageSettings = field(default_factory=StorageSettings)
    logging: LogConfig = field(
        default_factory=lambda: LogConfig(dir=str(linteum_home() / "logs"))
    )
    ui: UiSettings = field(default_factory=UiSettings)
    #: Unknown top-level keys preserved across read/write (forward compatibility).
    extra: dict = field(default_factory=dict, compare=False, repr=False)


# ---------------------------------------------------------------------------
# (de)serialization
# ---------------------------------------------------------------------------


def _default_sample(f) -> Any:
    if f.default is not MISSING:
        return f.default
    if f.default_factory is not MISSING:  # type: ignore[comparison-overlap]
        return f.default_factory()  # type: ignore[misc]
    return None


def _kind_for(cls: type, f, default: Any) -> str:
    override = _FIELD_KINDS.get((cls.__name__, f.name))
    if override is not None:
        return override
    if isinstance(default, bool):
        return "bool"
    if isinstance(default, int):
        return "int"
    if isinstance(default, float):
        return "float"
    if isinstance(default, str):
        return "str"
    if default is None:
        return "optional_str"
    return "any"


def _coerce(value: Any, kind: str, key: str) -> Any:
    """Coerce a YAML/env value into the field's scalar kind."""
    if kind == "any":
        return value
    if kind == "str":
        if value is None or isinstance(value, bool):
            raise ConfigError(f"{key} must be a string", key=key, code="E_CONFIG_TYPE")
        return str(value)
    if kind == "optional_str":
        if value is None:
            return None
        text = str(value)
        return text if text != "" else None
    if kind in ("int", "optional_int"):
        if value is None and kind == "optional_int":
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            raise ConfigError(f"{key} must be an integer", key=key, code="E_CONFIG_TYPE")
        try:
            number = int(value)
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"{key} must be an integer", key=key,
                              code="E_CONFIG_TYPE") from exc
        return number
    if kind in ("float", "optional_float"):
        if value is None and kind == "optional_float":
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            raise ConfigError(f"{key} must be a number", key=key, code="E_CONFIG_TYPE")
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"{key} must be a number", key=key,
                              code="E_CONFIG_TYPE") from exc
        if not math.isfinite(number):
            raise ConfigError(f"{key} must be finite", key=key, code="E_CONFIG_RANGE")
        return number
    if kind in ("bool", "optional_bool"):
        if value is None:
            if kind == "optional_bool":
                return None
            raise ConfigError(f"{key} must be a boolean", key=key, code="E_CONFIG_TYPE")
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if kind == "optional_bool" and text in _VALUE_NONE:
            return None
        if text in _VALUE_TRUE:
            return True
        if text in _VALUE_FALSE:
            return False
        raise ConfigError(f"{key} must be a boolean, got {value!r}",
                          key=key, code="E_CONFIG_TYPE")
    return value


def _hints(cls: type) -> dict:
    try:
        return get_type_hints(cls)
    except Exception:  # noqa: BLE001 - fall back to default-value inspection
        return {}


def _is_composite(annotation: Any) -> bool:
    origin = get_origin(annotation)
    return origin in (dict, list, tuple) or origin is ABCMapping


def _build_element(annotation: Any, value: Any, key: str) -> Any:
    if annotation is Any:
        return value
    if is_dataclass(annotation) and isinstance(annotation, type):
        return _build_dataclass(annotation, value, key)
    if annotation is bool:
        return _coerce(value, "bool", key)
    if annotation is int:
        return _coerce(value, "int", key)
    if annotation is float:
        return _coerce(value, "float", key)
    if annotation is str:
        return _coerce(value, "str", key)
    args = get_args(annotation)
    if type(None) in args:
        inner = next((item for item in args if item is not type(None)), None)
        if inner is bool:
            return _coerce(value, "optional_bool", key)
        if inner is int:
            return _coerce(value, "optional_int", key)
        if inner is float:
            return _coerce(value, "optional_float", key)
        if inner is str:
            return _coerce(value, "optional_str", key)
    return value


def _build_composite(annotation: Any, value: Any, key: str) -> Any:
    origin = get_origin(annotation)
    args = get_args(annotation)
    if origin is dict or origin is ABCMapping:
        if not isinstance(value, Mapping):
            raise ConfigError(f"{key} must be a mapping", key=key, code="E_CONFIG_TYPE")
        element = args[1] if len(args) >= 2 else Any
        return {
            str(name): _build_element(element, item, f"{key}.{name}")
            for name, item in value.items()
        }
    if origin in (list, tuple):
        if not isinstance(value, (list, tuple)):
            raise ConfigError(f"{key} must be a list", key=key, code="E_CONFIG_TYPE")
        element = args[0] if args else Any
        return tuple(
            _build_element(element, item, f"{key}[{index}]")
            for index, item in enumerate(value)
        )
    return value


def _build_dataclass(cls: type, raw: Any, path: str):
    """Build ``cls`` from a mapping, validating keys and scalar types."""
    if raw is None:
        raw = {}
    if not isinstance(raw, Mapping):
        raise ConfigError(f"{path or cls.__name__} must be a mapping",
                          key=path, code="E_CONFIG_TYPE")
    known = {f.name for f in fields(cls)}
    unknown = sorted(set(raw) - known)
    if unknown:
        raise ConfigError(
            f"unknown config key(s) under {path or cls.__name__}: {', '.join(unknown)}",
            key=path, code="E_CONFIG_UNKNOWN",
        )

    hints = _hints(cls)
    kwargs: dict[str, Any] = {}
    for f in fields(cls):
        key = f"{path}.{f.name}" if path else f.name
        if f.name not in raw:
            if f.default is not MISSING:
                kwargs[f.name] = f.default
                continue
            if f.default_factory is not MISSING:  # type: ignore[comparison-overlap]
                kwargs[f.name] = f.default_factory()  # type: ignore[misc]
                continue
            # Required field (LLMConfig.provider/model): pass None and let
            # validation surface a precise issue instead of a TypeError.
            kwargs[f.name] = None
            continue

        value = raw[f.name]
        annotation = hints.get(f.name)
        if annotation is not None and _is_composite(annotation):
            kwargs[f.name] = _build_composite(annotation, value, key)
            continue
        if annotation is not None and is_dataclass(annotation) and isinstance(annotation, type):
            kwargs[f.name] = _build_dataclass(annotation, value, key)
            continue

        # Fallback for unresolved hints: inspect the default value.
        sample = _default_sample(f)
        if f.default_factory is not MISSING:  # type: ignore[comparison-overlap]
            produced = f.default_factory()  # type: ignore[misc]
            if is_dataclass(produced) and not isinstance(produced, type):
                kwargs[f.name] = _build_dataclass(type(produced), value, key)
                continue
            sample = produced
        kwargs[f.name] = _coerce(value, _kind_for(cls, f, sample), key)
    return cls(**kwargs)


def app_from_dict(raw: Mapping[str, Any]) -> AppConfig:
    """Parse a YAML/JSON mapping into :class:`AppConfig`."""
    if not isinstance(raw, Mapping):
        raise ConfigError("config root must be a mapping", key="", code="E_CONFIG_PARSE")
    data = dict(raw)

    version = data.pop("version", CONFIG_VERSION)
    if isinstance(version, bool) or not isinstance(version, int):
        raise ConfigError("config version must be an integer", key="version",
                          code="E_CONFIG_VERSION")
    if version != CONFIG_VERSION:
        raise ConfigError(
            f"unsupported config version {version}; expected {CONFIG_VERSION}",
            key="version", code="E_CONFIG_VERSION",
            hint=f"delete the file or rewrite it with version: {CONFIG_VERSION}",
        )

    known_top = {"version", "agent", "gateway", "storage", "logging", "ui"}
    # Clear errors for the two most likely mistakes: the removed legacy layout
    # and putting the per-conversation workspace into the global config.
    if "llm" in data or "llms" in data:
        key = "llm" if "llm" in data else "llms"
        raise ConfigError(
            f"legacy top-level {key!r} is not supported; use agent.llm",
            key=key, code="E_CONFIG_LEGACY",
            hint="run `linteum config init --force` to write the new layout",
        )
    if "workspace" in data:
        raise ConfigError(
            "workspace is a per-session option, not a config key; "
            "pass it to session.create {workspace}",
            key="workspace", code="E_CONFIG_WORKSPACE",
        )
    extra = {key: value for key, value in data.items() if key not in known_top}
    sections = {key: value for key, value in data.items() if key in known_top}

    return AppConfig(
        version=version,
        agent=_build_dataclass(AgentSettings, sections.get("agent"), "agent"),
        gateway=_build_dataclass(GatewaySettings, sections.get("gateway"), "gateway"),
        storage=_build_dataclass(StorageSettings, sections.get("storage"), "storage"),
        logging=_build_dataclass(LogConfig, sections.get("logging"), "logging"),
        ui=_build_dataclass(UiSettings, sections.get("ui"), "ui"),
        extra=extra,
    )


def _to_dict(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return {f.name: _to_dict(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, tuple):
        return [_to_dict(item) for item in value]
    if isinstance(value, list):
        return [_to_dict(item) for item in value]
    if isinstance(value, Mapping):
        return {str(key): _to_dict(item) for key, item in value.items()}
    return value


def app_to_dict(config: AppConfig) -> dict:
    """Serialize :class:`AppConfig` into a plain YAML-friendly mapping."""
    data = {
        "version": config.version,
        "agent": _to_dict(config.agent),
        "gateway": _to_dict(config.gateway),
        "storage": _to_dict(config.storage),
        "logging": _to_dict(config.logging),
        "ui": _to_dict(config.ui),
    }
    for key, value in config.extra.items():
        data.setdefault(key, _to_dict(value))
    return data


def default_config() -> AppConfig:
    return AppConfig()


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _issue(issues: list[Issue], key: str, code: str, message: str,
           *, severity: str = "error", hint: str = "") -> None:
    issues.append(Issue(key=key, code=code, message=message,
                        severity=severity, hint=hint))


_PROFILE_NAME = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _resolve_env_text(value: Optional[str]) -> Optional[str]:
    """Resolve ``${VAR}`` for *comparison* only (the file keeps the reference)."""
    if not isinstance(value, str):
        return value
    return _ENV_REF.sub(
        lambda match: os.environ.get(match.group(1), match.group(0)), value
    )


def _resolved_api_key(llm: LLMConfig) -> Optional[str]:
    key = llm.api_key
    if isinstance(key, str) and "${" in key:
        resolved = _resolve_env_text(key)
        # A still-unresolved full reference means "use the provider env var";
        # for provider-conflict comparison that is equivalent to None.
        if _ENV_REF.fullmatch(resolved or ""):
            return None
        return resolved
    return llm.resolved_api_key()


def _resolved_base_url(llm: LLMConfig) -> Optional[str]:
    return _resolve_env_text(llm.resolved_base_url())


def _check_llm(issues: list[Issue], key: str, llm: LLMConfig, *,
               effective: bool = False) -> None:
    try:
        validate_llm_config(llm)
    except ValueError as exc:
        _issue(issues, key, "E_CONFIG_LLM", str(exc))
    if llm.provider not in PROVIDER_DEFAULTS and not llm.resolved_base_url():
        _issue(issues, f"{key}.provider", "E_CONFIG_PROVIDER",
               f"unknown provider {llm.provider!r} requires {key}.base_url")
    if llm.timeout is not None and llm.timeout <= 0:
        _issue(issues, f"{key}.timeout", "E_CONFIG_RANGE", "timeout must be > 0")
    if llm.max_tokens <= 0:
        _issue(issues, f"{key}.max_tokens", "E_CONFIG_RANGE", "max_tokens must be > 0")
    if llm.temperature is not None and not 0.0 <= llm.temperature <= 2.0:
        _issue(issues, f"{key}.temperature", "E_CONFIG_RANGE",
               "temperature must be within [0, 2]")
    if llm.top_p is not None and not 0.0 <= llm.top_p <= 1.0:
        _issue(issues, f"{key}.top_p", "E_CONFIG_RANGE", "top_p must be within [0, 1]")
    if effective and not llm.resolved_api_key():
        _issue(
            issues, f"{key}.api_key", "E_CONFIG_NO_API_KEY",
            f"no API key configured for {key}",
            severity="warning",
            hint="set the key, use ${ENV_VAR}, or export the provider's default key variable",
        )


def validate_config(config: AppConfig, *, effective: bool = False) -> list[Issue]:
    """Return all validation findings (errors and warnings)."""
    issues: list[Issue] = []

    if config.version != CONFIG_VERSION:
        _issue(issues, "version", "E_CONFIG_VERSION",
               f"unsupported config version {config.version}")

    # -- agent.llm + profiles + fallbacks -------------------------------
    agent = config.agent
    _check_llm(issues, "agent.llm", agent.llm, effective=effective)

    if not _PROFILE_NAME.match(agent.default_profile):
        _issue(issues, "agent.default_profile", "E_CONFIG_PROFILE_NAME",
               f"invalid default profile name {agent.default_profile!r}")
    elif (agent.default_profile != "default"
          and agent.default_profile not in agent.profiles):
        _issue(issues, "agent.default_profile", "E_CONFIG_PROFILE_NOT_FOUND",
               f"default profile {agent.default_profile!r} is not defined",
               hint=f"available profiles: {', '.join(agent.profile_names())}")

    for name, profile in agent.profiles.items():
        profile_key = f"agent.profiles.{name}"
        if name == "default":
            _issue(issues, profile_key, "E_CONFIG_PROFILE_RESERVED",
                   "'default' is reserved for agent.llm; use another profile name")
        elif not _PROFILE_NAME.match(name):
            _issue(issues, profile_key, "E_CONFIG_PROFILE_NAME",
                   f"invalid profile name {name!r}",
                   hint="use 1-64 characters from [A-Za-z0-9._-]")
        _check_llm(issues, profile_key, profile, effective=effective)

    for index, fallback in enumerate(agent.fallbacks):
        _check_llm(issues, f"agent.fallbacks.{index}", fallback, effective=effective)

    # One adapter is registered per provider.  Profiles sharing a provider
    # must therefore agree on endpoint / credentials / dialect; request
    # defaults differing between them are only a warning.
    entries = [("agent.llm", agent.llm)]
    entries.extend((f"agent.profiles.{name}", profile)
                   for name, profile in agent.profiles.items())
    entries.extend((f"agent.fallbacks.{index}", fallback)
                   for index, fallback in enumerate(agent.fallbacks))
    seen: dict[str, tuple[str, LLMConfig]] = {}
    params = ("timeout", "temperature", "max_tokens", "top_p",
              "reasoning_effort", "thinking")
    for entry_key, entry in entries:
        provider = entry.provider
        if not provider:
            continue
        if provider not in seen:
            seen[provider] = (entry_key, entry)
            continue
        first_key, first = seen[provider]
        if (_resolved_base_url(first) != _resolved_base_url(entry)
                or _resolved_api_key(first) != _resolved_api_key(entry)
                or first.resolved_openai_mode() != entry.resolved_openai_mode()):
            _issue(issues, entry_key, "E_CONFIG_PROVIDER_CONFLICT",
                   f"provider {provider!r} is configured differently in "
                   f"{first_key} and {entry_key}",
                   hint="use distinct provider names (with base_url) for "
                        "different endpoints or credentials")
        if any(getattr(first, name) != getattr(entry, name) for name in params):
            _issue(issues, entry_key, "E_CONFIG_PROVIDER_PARAMS",
                   f"provider {provider!r} has different request defaults in "
                   f"{first_key} and {entry_key}",
                   severity="warning",
                   hint="one adapter is registered per provider; use distinct "
                        "provider names if this difference must be honored")

    # -- agent.context --------------------------------------------------
    ctx = config.agent.context
    if ctx.policy not in ("passthrough", "compacting"):
        _issue(issues, "agent.context.policy", "E_CONFIG_ENUM",
               f"unknown context policy {ctx.policy!r}")
    if ctx.max_tokens < 0:
        _issue(issues, "agent.context.max_tokens", "E_CONFIG_RANGE", "must be >= 0")
    if ctx.compact_to_tokens < 0:
        _issue(issues, "agent.context.compact_to_tokens", "E_CONFIG_RANGE", "must be >= 0")
    if ctx.keep_last_messages < 1:
        _issue(issues, "agent.context.keep_last_messages", "E_CONFIG_RANGE", "must be >= 1")
    if ctx.max_summary_tokens < 1:
        _issue(issues, "agent.context.max_summary_tokens", "E_CONFIG_RANGE", "must be >= 1")
    if ctx.policy == "compacting":
        if ctx.max_tokens <= 0:
            _issue(issues, "agent.context.max_tokens", "E_CONFIG_RANGE",
                   "compacting policy requires max_tokens > 0")
        if ctx.max_tokens > 0 and ctx.compact_to_tokens >= ctx.max_tokens:
            _issue(issues, "agent.context.compact_to_tokens", "E_CONFIG_RANGE",
                   "compact_to_tokens must be < max_tokens")

    # -- agent.tools ----------------------------------------------------
    if config.agent.tools.approval not in ("ask", "allow", "deny"):
        _issue(issues, "agent.tools.approval", "E_CONFIG_ENUM",
               f"unknown approval policy {config.agent.tools.approval!r}")
    if config.agent.tools.bash_timeout <= 0:
        _issue(issues, "agent.tools.bash_timeout", "E_CONFIG_RANGE", "must be > 0")

    # -- agent.skills ---------------------------------------------------
    skills = config.agent.skills
    for name in (
        "max_index_bytes", "max_result_bytes", "max_skill_bytes",
        "max_resource_bytes", "max_resources_per_skill", "max_skills",
        "max_catalog_bytes",
    ):
        if getattr(skills, name) <= 0:
            _issue(issues, f"agent.skills.{name}", "E_CONFIG_RANGE", "must be > 0")
    if skills.max_index_bytes < 512:
        _issue(issues, "agent.skills.max_index_bytes", "E_CONFIG_RANGE",
               "must be >= 512 so the fixed index header can fit")
    if skills.max_result_bytes + 512 >= 8192:
        _issue(issues, "agent.skills.max_result_bytes", "E_CONFIG_RANGE",
               "must be < 7680 (ToolRuntime externalises output above 8 KiB)")
    project_dir = Path(skills.project_dir)
    if (not skills.project_dir or project_dir.is_absolute()
            or ".." in project_dir.parts):
        _issue(issues, "agent.skills.project_dir", "E_CONFIG_RANGE",
               "must be a relative path without '..'")
    for name in ("system_dirs", "extra_dirs"):
        for index, path in enumerate(getattr(skills, name)):
            if not isinstance(path, str) or not path.strip():
                _issue(issues, f"agent.skills.{name}.{index}", "E_CONFIG_RANGE",
                       "must be a non-empty string")

    # -- agent.budget ---------------------------------------------------
    budget = config.agent.budget
    for name in ("tokens", "wall_ms", "rounds"):
        if getattr(budget, name) < 0:
            _issue(issues, f"agent.budget.{name}", "E_CONFIG_RANGE", "must be >= 0")
    if budget.usd < 0:
        _issue(issues, "agent.budget.usd", "E_CONFIG_RANGE", "must be >= 0")

    # -- agent.session --------------------------------------------------
    session = config.agent.session
    if session.input_policy not in ("queue", "interrupt", "reject"):
        _issue(issues, "agent.session.input_policy", "E_CONFIG_ENUM",
               f"unknown input policy {session.input_policy!r}")
    if session.idle_ttl_ms < 0:
        _issue(issues, "agent.session.idle_ttl_ms", "E_CONFIG_RANGE", "must be >= 0")

    # -- gateway --------------------------------------------------------
    gateway = config.gateway
    if gateway.transport not in ("auto", "inproc", "stdio", "websocket"):
        _issue(issues, "gateway.transport", "E_CONFIG_ENUM",
               f"unknown transport {gateway.transport!r}")
    if not gateway.websocket.host:
        _issue(issues, "gateway.websocket.host", "E_CONFIG_RANGE", "host must not be empty")
    if not 1 <= gateway.websocket.port <= 65535:
        _issue(issues, "gateway.websocket.port", "E_CONFIG_RANGE",
               "port must be within [1, 65535]")
    for name in ("client_queue_frames", "client_queue_bytes",
                 "max_command_bytes", "max_frame_bytes"):
        if getattr(gateway.limits, name) <= 0:
            _issue(issues, f"gateway.limits.{name}", "E_CONFIG_RANGE", "must be > 0")
    if gateway.delta.coalesce_window_ms < 0:
        _issue(issues, "gateway.delta.coalesce_window_ms", "E_CONFIG_RANGE", "must be >= 0")
    if gateway.history.page_size <= 0:
        _issue(issues, "gateway.history.page_size", "E_CONFIG_RANGE", "must be > 0")
    if gateway.history.max_page_bytes <= 0:
        _issue(issues, "gateway.history.max_page_bytes", "E_CONFIG_RANGE", "must be > 0")
    if not 0.0 <= gateway.backpressure.ephemeral_high_watermark <= 1.0:
        _issue(issues, "gateway.backpressure.ephemeral_high_watermark", "E_CONFIG_RANGE",
               "must be within [0, 1]")

    # -- storage --------------------------------------------------------
    if not config.storage.state_db:
        _issue(issues, "storage.state_db", "E_CONFIG_RANGE", "must not be empty")
    if not config.storage.artifacts:
        _issue(issues, "storage.artifacts", "E_CONFIG_RANGE", "must not be empty")
    if config.storage.checkpoints.keep < 0:
        _issue(issues, "storage.checkpoints.keep", "E_CONFIG_RANGE", "must be >= 0")
    if config.storage.checkpoints.every_n_events < 0:
        _issue(issues, "storage.checkpoints.every_n_events", "E_CONFIG_RANGE", "must be >= 0")

    # -- logging --------------------------------------------------------
    logging = config.logging
    if logging.level not in ("debug", "info", "warning", "error"):
        _issue(issues, "logging.level", "E_CONFIG_ENUM",
               f"unknown log level {logging.level!r}")
    if logging.format not in ("text", "json"):
        _issue(issues, "logging.format", "E_CONFIG_ENUM",
               f"unknown log format {logging.format!r}")
    if logging.max_bytes < 0:
        _issue(issues, "logging.max_bytes", "E_CONFIG_RANGE", "must be >= 0")
    if logging.backups < 0:
        _issue(issues, "logging.backups", "E_CONFIG_RANGE", "must be >= 0")

    # -- ui -------------------------------------------------------------
    if not config.ui.language:
        _issue(issues, "ui.language", "E_CONFIG_RANGE", "must not be empty")
    if config.ui.theme not in ("auto", "light", "dark"):
        _issue(issues, "ui.theme", "E_CONFIG_ENUM", f"unknown theme {config.ui.theme!r}")

    return issues


def raise_for_issues(issues: list[Issue]) -> None:
    """Raise the first error as :class:`ConfigError`; warnings are ignored."""
    for issue in issues:
        if issue.is_error:
            raise ConfigError(issue.message, key=issue.key,
                              code=issue.code, hint=issue.hint)


__all__ = [
    "CONFIG_VERSION",
    "DEFAULT_HOME",
    "DEFAULT_MODEL",
    "DEFAULT_PROVIDER",
    "AgentSettings",
    "AppConfig",
    "BackpressureSettings",
    "BudgetSettings",
    "CheckpointSettings",
    "ConfigError",
    "ContextSettings",
    "DeltaSettings",
    "GatewayLimits",
    "GatewaySettings",
    "HistorySettings",
    "Issue",
    "SessionSettings",
    "SkillsSettings",
    "StorageSettings",
    "ToolSettings",
    "UiSettings",
    "WebSocketSettings",
    "app_from_dict",
    "app_to_dict",
    "default_config",
    "raise_for_issues",
    "validate_config",
]
