"""配置文件读写：YAML 格式，默认保存到用户主目录的 ``~/.agent`` 目录。

用法::

    from linteum_agent.config import ConfigStore, LLMConfig

    store = ConfigStore()             # 默认 -> ~/.agent/config.yaml
    config = store.load(create=True)  # 文件不存在时先写入默认配置再读取

    config.set_llm(
        LLMConfig(
            provider="openai_compatible",
            model="deepseek-chat",
            base_url="https://api.deepseek.com",
            api_key="sk-xxx",
        )
    )
    store.save()

    store.llm()            # 唯一的 LLM（已规范化为 LLMConfig）

路径优先级：``ConfigStore(path=...)`` > 环境变量 ``linteum_agent_CONFIG`` >
``~/.agent/config.yaml``。写入使用“临时文件 + ``os.replace``”原子替换；
配置里出现 ``api_key`` 时会把文件权限尽量收紧到 ``0600``（失败时忽略）。
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from threading import RLock
from typing import Any

import yaml

try:  # 包内导入
    from .schema import (CONFIG_VERSION, Config, ConfigError, LLMConfig,
                         empty_config)
except ImportError:  # 支持直接将 /workspace/linteum_agent 加入 sys.path
    from schema import (CONFIG_VERSION, Config, ConfigError, LLMConfig,
                        empty_config)


# 用户级状态目录名（配置 / 快照 / 会话等都放在用户主目录的这个目录下，即 ``~/.agent``）。
AGENT_DIRNAME = ".agent"
# 默认配置文件名（位于用户主目录的 ``.agent`` 下）。
DEFAULT_CONFIG_FILENAME = "config.yaml"
# 环境变量：覆盖默认配置文件路径。
CONFIG_PATH_ENV = "linteum_agent_CONFIG"
# 出现这些字段时收紧文件权限（0600）。
_SECRET_FIELDS = ("api_key",)

# 新建配置文件时写入的注释头，方便手工编辑。
CONFIG_HEADER = """\
# linteum_agent 项目配置文件（YAML）
#
# llm:       唯一的 LLM 配置；api_key / base_url 可留空，由 provider 回退到环境变量
#            （OPENAI_API_KEY / DEEPSEEK_API_KEY / ANTHROPIC_API_KEY 等）。
#            所有 LLM 配置都必须能被 LLMConfig 规范化；不支持多套配置（llms）。
#
# 支持的 provider: openai / openai_compatible / anthropic / deepseek / deepseek_compatible
# 提示：本文件可能保存 API Key，建议保留在 .gitignore 中，或改用环境变量。
"""


def project_root() -> Path:
    """返回项目根目录：``linteum_agent`` 包的上一级目录。

    注意：这只是代码仓库 / 安装位置，**不再**作为用户状态的存储位置
    （配置 / 快照 / 会话都已改到 :func:`agent_home`）；保留它仅用于展示与旧位置迁移。
    """
    return Path(__file__).resolve().parents[2]


def agent_home() -> Path:
    """用户级 agent 主目录：``~/.agent``。

    配置（``config.yaml``）、CLI 快照（``snapshot.json``）、当前会话指针
    （``current_session``）以及用户级会话副本（``sessions/``）都放在这里，
    不再写进项目仓库 / 安装目录。
    """
    return Path.home() / AGENT_DIRNAME


def default_config_path() -> Path:
    """返回默认配置文件路径。

    优先级：环境变量 ``linteum_agent_CONFIG`` > ``~/.agent/config.yaml``。

    默认落在用户主目录的 ``.agent``（用户级状态目录）下，与会话 / 快照同处一地；
    首次使用时由 ``linteum setup`` 生成。
    """
    override = os.getenv(CONFIG_PATH_ENV)
    if override and override.strip():
        return Path(override).expanduser()
    return agent_home() / DEFAULT_CONFIG_FILENAME


def to_yaml(config: Config) -> str:
    """把 ``Config`` 序列化为 YAML 文本（保持字段顺序、允许中文）。"""
    return yaml.safe_dump(
        config.to_dict(),
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
    )


def default_config_yaml(config: Config | None = None) -> str:
    """生成默认配置文件内容：注释头 + 默认配置。"""
    return CONFIG_HEADER + to_yaml(config if config is not None else empty_config())


class ConfigStore:
    """项目配置文件的读写入口（YAML）。

    实例本身是“配置 + 路径”的组合：读取后可通过 ``store.config`` 访问当前配置，
    修改后调用 ``store.save()`` 落盘。所有操作都在同一把可重入锁内完成。
    """

    VERSION = CONFIG_VERSION

    def __init__(self, path: str | Path | None = None, *, autoload: bool = False):
        self._lock = RLock()
        self.path = Path(path).expanduser() if path is not None else default_config_path()
        self._config = empty_config()
        self._loaded = False
        if autoload:
            self.load()

    # ------------------------------------------------------------------
    # 读写
    # ------------------------------------------------------------------

    def exists(self, path: str | Path | None = None) -> bool:
        """配置文件是否已存在。"""
        return self._target(path).is_file()

    def load(
        self,
        path: str | Path | None = None,
        *,
        create: bool = False,
    ) -> Config:
        """读取配置文件并缓存结果。

        - 文件不存在且 ``create=True``：先写入默认配置文件，再读取返回；
        - 文件不存在且 ``create=False``：返回内存中的默认配置，不落盘；
        - 文件为空：视为默认配置；解析失败 / 内容非法时抛 ``ConfigError``。
        """
        with self._lock:
            target = self._target(path)
            if not target.is_file():
                config = empty_config()
                if create:
                    self._write(target, config)
                self._assign(target, config)
                return config

            config = self._read(target)
            self._assign(target, config)
            return config

    def reload(self) -> Config:
        """按当前路径重新读取配置。"""
        return self.load(self.path)

    def save(
        self,
        config: Config | None = None,
        path: str | Path | None = None,
    ) -> Path:
        """把配置写入 YAML 文件并返回实际写入路径。

        ``config`` 为空时写入当前缓存的配置；显式传入 ``path`` 时目标路径以它为准。
        """
        with self._lock:
            target = self._target(path)
            if config is None:
                if self._loaded and target == self.path:
                    config = self._config
                else:
                    config = self.load(target)
            else:
                config = self._coerce_config(config)
            self._write(target, config)
            self._assign(target, config)
            return target

    # ------------------------------------------------------------------
    # 当前配置
    # ------------------------------------------------------------------

    @property
    def config(self) -> Config:
        """当前配置；尚未加载时自动读取（文件不存在则用默认配置）。"""
        with self._lock:
            if not self._loaded:
                self.load()
            return self._config

    def set(self, config: Config) -> Config:
        """替换当前内存配置（不落盘）。"""
        with self._lock:
            self._config = self._coerce_config(config)
            self._loaded = True
            return self._config

    # ------------------------------------------------------------------
    # 便捷访问
    # ------------------------------------------------------------------

    def llm(self) -> LLMConfig:
        """返回配置里的 LLM（``Config.llm``，已是 ``LLMConfig``）。"""
        return self.config.llm

    def set_llm(self, config: LLMConfig) -> Config:
        """写入（内存中的）LLM 配置；需要落盘时再调用 ``save()``。"""
        return self.config.set_llm(config)

    # ------------------------------------------------------------------
    # 内部实现
    # ------------------------------------------------------------------

    def _target(self, path: str | Path | None) -> Path:
        """归一化目标路径：显式传入时优先，否则用当前路径。"""
        if path is None:
            return self.path
        return Path(path).expanduser()

    def _assign(self, target: Path, config: Config) -> None:
        """记录“当前路径 + 当前配置”。"""
        self.path = target
        self._config = config
        self._loaded = True

    @staticmethod
    def _coerce_config(config: Any) -> Config:
        if isinstance(config, Config):
            return config
        if isinstance(config, dict):
            return Config.from_dict(config)
        raise ConfigError(f"config 需要 Config 或 dict，实际为 {type(config).__name__}")

    @staticmethod
    def _read(target: Path) -> Config:
        try:
            raw = target.read_text(encoding="utf-8")
        except OSError as exc:
            raise ConfigError(f"读取配置文件失败: {target} ({exc})") from exc

        if not raw.strip():
            return empty_config()
        try:
            data = yaml.safe_load(raw)
        except yaml.YAMLError as exc:
            raise ConfigError(f"解析 YAML 失败: {target} ({exc})") from exc
        if data is None:
            return empty_config()
        if not isinstance(data, dict):
            raise ConfigError(f"配置文件顶层需要 mapping: {target}")
        return Config.from_dict(data)

    def _write(self, target: Path, config: Config) -> None:
        """原子写入：先写同目录临时文件，再 ``os.replace`` 覆盖目标。

        文件内容 = 注释头（``CONFIG_HEADER``）+ YAML 正文，便于手工编辑；
        YAML 解析器会忽略注释。
        """
        target = target.expanduser()
        text = CONFIG_HEADER + to_yaml(config)
        target.parent.mkdir(parents=True, exist_ok=True)

        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text)
            if _has_secret(config):
                try:
                    os.chmod(tmp_name, 0o600)
                except OSError:  # 例如 Windows 上不支持
                    pass
            os.replace(tmp_name, target)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    def __repr__(self) -> str:  # pragma: no cover - 仅调试用
        return f"ConfigStore(path={str(self.path)!r}, loaded={self._loaded})"


def _has_secret(config: Config) -> bool:
    """配置里是否包含 API Key（用于收紧文件权限）。"""
    return any(getattr(config.llm, field, None) for field in _SECRET_FIELDS)


# ----------------------------------------------------------------------
# 默认 store 与模块级便捷函数
# ----------------------------------------------------------------------

_default_store: ConfigStore | None = None
_default_store_lock = RLock()


def get_config_store(
    path: str | Path | None = None,
    *,
    reload: bool = False,
) -> ConfigStore:
    """返回进程级默认 ``ConfigStore``；传入 ``path`` 时会重建。

    ``reload=True`` 时按当前路径重新读取配置文件（未指定 ``path`` 时生效）。
    """
    global _default_store
    with _default_store_lock:
        if _default_store is None or path is not None:
            _default_store = ConfigStore(path)
        elif reload:
            _default_store.reload()
        return _default_store


def set_config_store(store: ConfigStore) -> ConfigStore:
    """替换进程级默认 ``ConfigStore``（测试或多项目切换时使用）。"""
    global _default_store
    if not isinstance(store, ConfigStore):
        raise ConfigError(f"store 需要 ConfigStore，实际为 {type(store).__name__}")
    with _default_store_lock:
        _default_store = store
    return store


def load_config(path: str | Path | None = None, *, create: bool = True) -> Config:
    """读取配置（默认 ``~/.agent/config.yaml``；不存在时生成默认配置文件）。"""
    return get_config_store(path).load(create=create)


def save_config(config: Config, path: str | Path | None = None) -> Path:
    """写入配置并返回实际写入路径。"""
    return get_config_store(path).save(config)

