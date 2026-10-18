"""Semi-permanent configuration for ``linteum_agent``.

The config layer is intentionally independent of the event bus and the Gateway:

* :mod:`~linteum_agent.config.schema` — dataclasses, YAML round-trip, validation;
* :mod:`~linteum_agent.config.meta` — :class:`FieldSpec` metadata for UIs;
* :mod:`~linteum_agent.config.store` — atomic YAML reads/writes and backups;
* :mod:`~linteum_agent.config.service` — source merging, sources, apply scopes;
* :mod:`~linteum_agent.config.options` — :class:`SessionOptions` (per-conversation
  workspace and profile; **not** stored in the config file).

Quick start::

    from linteum_agent.config import ConfigService, SessionOptions

    service = ConfigService()                  # ~/.linteum/config.yaml
    service.ensure()                           # write defaults on first use
    effective = service.effective()            # defaults < file < env < overrides
    service.set("agent.llm.model", "deepseek-chat")

    options = SessionOptions(workspace="/path/to/project", session_id="s-01")

The workspace is a session-level parameter: every conversation can point at a
different directory, so it is deliberately absent from ``AppConfig``.
"""

from ..runtime.llm.config import LLMConfig
from ..runtime.observability.config import LogConfig
from .meta import (
    FieldSpec,
    apply_scope,
    build_schema,
    coerce_value,
    field_specs,
    get_spec,
)
from .options import SessionOptions
from .schema import (
    CONFIG_VERSION,
    DEFAULT_HOME,
    DEFAULT_MODEL,
    DEFAULT_PROVIDER,
    AgentSettings,
    AppConfig,
    BackpressureSettings,
    BudgetSettings,
    CheckpointSettings,
    ConfigError,
    ContextSettings,
    DeltaSettings,
    GatewayLimits,
    GatewaySettings,
    HistorySettings,
    Issue,
    SessionSettings,
    StorageSettings,
    ToolSettings,
    UiSettings,
    WebSocketSettings,
    app_from_dict,
    app_to_dict,
    default_config,
    raise_for_issues,
    validate_config,
)
from .service import (
    ENV_PREFIX,
    ApplyResult,
    ChangeSet,
    ConfigChange,
    ConfigService,
    ConfigValue,
    EffectiveConfig,
    env_var_for,
)
from .paths import (
    AGENT_DIRNAME,
    CONFIG_PATH_ENV,
    DEFAULT_CONFIG_FILENAME,
    HOME_ENV,
    default_config_path,
    linteum_home,
)
from .store import ConfigStore, default_config_yaml, to_yaml

__all__ = [
    # schema
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
    "LLMConfig",
    "LogConfig",
    "SessionSettings",
    "StorageSettings",
    "ToolSettings",
    "UiSettings",
    "WebSocketSettings",
    "app_from_dict",
    "app_to_dict",
    "default_config",
    "raise_for_issues",
    "validate_config",
    # metadata
    "FieldSpec",
    "apply_scope",
    "build_schema",
    "coerce_value",
    "field_specs",
    "get_spec",
    # store
    "AGENT_DIRNAME",
    "CONFIG_PATH_ENV",
    "DEFAULT_CONFIG_FILENAME",
    "HOME_ENV",
    "ConfigStore",
    "default_config_path",
    "default_config_yaml",
    "linteum_home",
    "to_yaml",
    # service
    "ENV_PREFIX",
    "ApplyResult",
    "ChangeSet",
    "ConfigChange",
    "ConfigService",
    "ConfigValue",
    "EffectiveConfig",
    "env_var_for",
    # per-session options
    "SessionOptions",
]
