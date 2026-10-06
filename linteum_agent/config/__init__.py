"""项目配置层：配置文件的读取与写入（YAML），默认保存在用户主目录 ``~/.agent`` 下。

快速上手::

    from linteum_agent.config import ConfigStore, LLMConfig

    store = ConfigStore()             # 默认 -> ~/.agent/config.yaml
    config = store.load(create=True)  # 文件不存在时先写入默认配置再读取

    config.set_llm(LLMConfig(provider="openai", model="gpt-4o-mini", api_key="sk-xxx"))
    store.save()

    store.llm().model          # 'gpt-4o-mini'（唯一的 LLM，已规范化为 LLMConfig）

模块划分：

- ``schema``：``Config`` 数据模型与 LLM 配置的 (反)序列化 / 校验；
- ``store``：``ConfigStore`` 负责 YAML 文件读写，以及模块级
  ``load_config`` / ``save_config`` / ``get_config_store`` 便捷函数。

配置里可直接使用 OpenAI / Anthropic / OpenAI 兼容接口（含 DeepSeek 等），
``api_key`` 与 ``base_url`` 均为可选项（省略时由 provider 回退到环境变量）。

配置文件里只有一处 ``llm``：所有 LLM 配置都必须先规范化为 ``LLMConfig``
（``llm_from_dict`` + ``validate_llm_config``）才能被加载，不支持 ``llms``
这类「多套配置按名字取」的写法（出现该键会直接报错）。
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
    # 数据模型
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
    # 文件读写
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
