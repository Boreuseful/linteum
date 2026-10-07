"""Project config layer: reading and writing the config file (YAML), saved under the user home directory ``~/.agent`` by default.

Quick start::

    from linteum_agent.config import ConfigStore, LLMConfig

    store = ConfigStore()             # default -> ~/.agent/config.yaml
    config = store.load(create=True)  # write the default config first if the file is missing, then read

    config.set_llm(LLMConfig(provider="openai", model="gpt-4o-mini", api_key="sk-xxx"))
    store.save()

    store.llm().model          # 'gpt-4o-mini' (the only LLM, normalized to LLMConfig)

Module layout:

- ``schema``: the ``Config`` data model and (de)serialization / validation of the LLM config;
- ``store``: ``ConfigStore`` handles YAML file read/write, plus the module-level
  ``load_config`` / ``save_config`` / ``get_config_store`` convenience functions.

The config can directly use OpenAI / Anthropic / OpenAI-compatible endpoints (including DeepSeek,
etc.); ``api_key`` and ``base_url`` are both optional (when omitted, the provider falls back to
environment variables).

There is only one ``llm`` section in the config file: all LLM config must first be normalized to
``LLMConfig`` (``llm_from_dict`` + ``validate_llm_config``) before it can be loaded; the ``llms``
style of "multiple configs fetched by name" is not supported (that key raises an error directly).
"""

from .schema import (CONFIG_VERSION, DEFAULT_MODEL, DEFAULT_PROVIDER,
                     SUPPORTED_OPENAI_MODES, SUPPORTED_PROVIDERS, Config,
                     ConfigError, LLMConfig, default_llm_config,
                     empty_config, llm_from_dict, llm_to_dict,
                     validate_llm_config)
from .store import (AGENT_DIRNAME, CONFIG_HEADER, CONFIG_PATH_ENV,
                    DEFAULT_CONFIG_FILENAME, ConfigStore, agent_home,
                    default_config_path, default_config_yaml, get_config_store,
                    load_config, project_root, save_config, set_config_store,
                    to_yaml)

__all__ = [
    # Data model
    "Config",
    "ConfigError",
    "LLMConfig",
    "CONFIG_VERSION",
    "DEFAULT_MODEL",
    "DEFAULT_PROVIDER",
    "SUPPORTED_PROVIDERS",
    "SUPPORTED_OPENAI_MODES",
    "default_llm_config",
    "empty_config",
    "llm_from_dict",
    "llm_to_dict",
    "validate_llm_config",
    # File read/write
    "ConfigStore",
    "AGENT_DIRNAME",
    "CONFIG_HEADER",
    "CONFIG_PATH_ENV",
    "DEFAULT_CONFIG_FILENAME",
    "agent_home",
    "default_config_path",
    "default_config_yaml",
    "get_config_store",
    "load_config",
    "project_root",
    "save_config",
    "set_config_store",
    "to_yaml",
]
