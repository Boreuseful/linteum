"""Config file read/write: YAML format, saved to the user home directory's ``~/.agent`` directory by default.

Usage::

    from linteum_agent.config import ConfigStore, LLMConfig

    store = ConfigStore()             # default -> ~/.agent/config.yaml
    config = store.load(create=True)  # write the default config first if the file is missing, then read

    config.set_llm(
        LLMConfig(
            provider="openai_compatible",
            model="deepseek-chat",
            base_url="https://api.deepseek.com",
            api_key="sk-xxx",
        )
    )
    store.save()

    store.llm()            # the only LLM (already normalized to LLMConfig)

Path priority: ``ConfigStore(path=...)`` > the environment variable ``linteum_agent_CONFIG`` >
``~/.agent/config.yaml``. Writing uses "temp file + ``os.replace``" for an atomic replace; when the
config contains ``api_key``, the file permissions are tightened to ``0600`` as much as possible
(ignored on failure).
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from threading import RLock
from typing import Any

import yaml

try:  # intra-package import
    from .schema import (CONFIG_VERSION, Config, ConfigError, LLMConfig,
                         empty_config)
except ImportError:  # supports adding /workspace/linteum_agent to sys.path directly
    from schema import (CONFIG_VERSION, Config, ConfigError, LLMConfig,
                        empty_config)


# User-level state directory name (config / snapshot / sessions all live under this directory in the
# user home directory, i.e. ``~/.agent``).
AGENT_DIRNAME = ".agent"
# Default config file name (under ``.agent`` in the user home directory).
DEFAULT_CONFIG_FILENAME = "config.yaml"
# Environment variable: overrides the default config file path.
CONFIG_PATH_ENV = "linteum_agent_CONFIG"
# Tighten file permissions (0600) when these fields are present.
_SECRET_FIELDS = ("api_key",)

# Comment header written when creating a new config file, for easy manual editing.
CONFIG_HEADER = """\
# linteum_agent project config file (YAML)
#
# llm:       the single LLM config; api_key / base_url may be left empty and fall back to
#            environment variables via the provider
#            (OPENAI_API_KEY / DEEPSEEK_API_KEY / ANTHROPIC_API_KEY, etc.).
#            All LLM config must be normalizable by LLMConfig; multiple configs (llms) are not supported.
#
# Supported providers: openai / openai_compatible / anthropic / deepseek / deepseek_compatible
# Tip: this file may store an API Key; keep it in .gitignore, or use environment variables instead.
"""


def project_root() -> Path:
    """Return the project root: the parent directory of the ``linteum_agent`` package.

    Note: this is only the code repository / install location and is **no longer** the storage
    location for user state (config / snapshots / sessions have all moved to :func:`agent_home`); it
    is kept only for display and for migrating old locations.
    """
    return Path(__file__).resolve().parents[2]


def agent_home() -> Path:
    """User-level agent home directory: ``~/.agent``.

    The config (``config.yaml``), CLI snapshot (``snapshot.json``), current-session pointer
    (``current_session``), and user-level session copies (``sessions/``) all live here, and are no
    longer written into the project repository / install directory.
    """
    return Path.home() / AGENT_DIRNAME


def default_config_path() -> Path:
    """Return the default config file path.

    Priority: the environment variable ``linteum_agent_CONFIG`` > ``~/.agent/config.yaml``.

    It defaults to the ``.agent`` directory (user-level state directory) under the user home
    directory, alongside sessions / snapshots; generated on first use by ``linteum setup``.
    """
    override = os.getenv(CONFIG_PATH_ENV)
    if override and override.strip():
        return Path(override).expanduser()
    return agent_home() / DEFAULT_CONFIG_FILENAME


def to_yaml(config: Config) -> str:
    """Serialize ``Config`` into YAML text (preserving field order and allowing non-ASCII)."""
    return yaml.safe_dump(
        config.to_dict(),
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
    )


def default_config_yaml(config: Config | None = None) -> str:
    """Generate the default config file content: comment header + default config."""
    return CONFIG_HEADER + to_yaml(config if config is not None else empty_config())


class ConfigStore:
    """The read/write entry point for the project config file (YAML).

    An instance is a combination of "config + path": after loading, the current config is accessible
    via ``store.config``; after modifying, call ``store.save()`` to persist. All operations complete
    within the same reentrant lock.
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
    # Read/write
    # ------------------------------------------------------------------

    def exists(self, path: str | Path | None = None) -> bool:
        """Whether the config file already exists."""
        return self._target(path).is_file()

    def load(
        self,
        path: str | Path | None = None,
        *,
        create: bool = False,
    ) -> Config:
        """Read the config file and cache the result.

        - File missing and ``create=True``: write the default config file first, then read and return;
        - File missing and ``create=False``: return the in-memory default config without writing;
        - Empty file: treated as the default config; parse failure / invalid content raises
          ``ConfigError``.
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
        """Re-read the config from the current path."""
        return self.load(self.path)

    def save(
        self,
        config: Config | None = None,
        path: str | Path | None = None,
    ) -> Path:
        """Write the config to a YAML file and return the actual written path.

        When ``config`` is empty, write the currently cached config; when ``path`` is explicitly
        passed, it takes precedence as the target path.
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
    # Current config
    # ------------------------------------------------------------------

    @property
    def config(self) -> Config:
        """The current config; auto-loads when not yet loaded (uses the default config if the file is missing)."""
        with self._lock:
            if not self._loaded:
                self.load()
            return self._config

    def set(self, config: Config) -> Config:
        """Replace the current in-memory config (does not write to disk)."""
        with self._lock:
            self._config = self._coerce_config(config)
            self._loaded = True
            return self._config

    # ------------------------------------------------------------------
    # Convenience access
    # ------------------------------------------------------------------

    def llm(self) -> LLMConfig:
        """Return the LLM in the config (``Config.llm``, already a ``LLMConfig``)."""
        return self.config.llm

    def set_llm(self, config: LLMConfig) -> Config:
        """Write the (in-memory) LLM config; call ``save()`` when it needs to be persisted."""
        return self.config.set_llm(config)

    # ------------------------------------------------------------------
    # Internal implementation
    # ------------------------------------------------------------------

    def _target(self, path: str | Path | None) -> Path:
        """Normalize the target path: an explicit path takes precedence, otherwise use the current path."""
        if path is None:
            return self.path
        return Path(path).expanduser()

    def _assign(self, target: Path, config: Config) -> None:
        """Record "current path + current config"."""
        self.path = target
        self._config = config
        self._loaded = True

    @staticmethod
    def _coerce_config(config: Any) -> Config:
        if isinstance(config, Config):
            return config
        if isinstance(config, dict):
            return Config.from_dict(config)
        raise ConfigError(f"config must be a Config or dict, got {type(config).__name__}")

    @staticmethod
    def _read(target: Path) -> Config:
        try:
            raw = target.read_text(encoding="utf-8")
        except OSError as exc:
            raise ConfigError(f"failed to read config file: {target} ({exc})") from exc

        if not raw.strip():
            return empty_config()
        try:
            data = yaml.safe_load(raw)
        except yaml.YAMLError as exc:
            raise ConfigError(f"failed to parse YAML: {target} ({exc})") from exc
        if data is None:
            return empty_config()
        if not isinstance(data, dict):
            raise ConfigError(f"config file top level must be a mapping: {target}")
        return Config.from_dict(data)

    def _write(self, target: Path, config: Config) -> None:
        """Atomic write: write a temp file in the same directory first, then ``os.replace`` the target.

        File content = comment header (``CONFIG_HEADER``) + YAML body, for easy manual editing; the
        YAML parser ignores comments.
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
                except OSError:  # e.g. unsupported on Windows
                    pass
            os.replace(tmp_name, target)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    def __repr__(self) -> str:  # pragma: no cover - for debugging only
        return f"ConfigStore(path={str(self.path)!r}, loaded={self._loaded})"


def _has_secret(config: Config) -> bool:
    """Whether the config contains an API key (used to tighten file permissions)."""
    return any(getattr(config.llm, field, None) for field in _SECRET_FIELDS)


# ----------------------------------------------------------------------
# Default store and module-level convenience functions
# ----------------------------------------------------------------------

_default_store: ConfigStore | None = None
_default_store_lock = RLock()


def get_config_store(
    path: str | Path | None = None,
    *,
    reload: bool = False,
) -> ConfigStore:
    """Return the process-level default ``ConfigStore``; passing ``path`` rebuilds it.

    With ``reload=True`` the config file is re-read from the current path (effective when ``path`` is
    not given).
    """
    global _default_store
    with _default_store_lock:
        if _default_store is None or path is not None:
            _default_store = ConfigStore(path)
        elif reload:
            _default_store.reload()
        return _default_store


def set_config_store(store: ConfigStore) -> ConfigStore:
    """Replace the process-level default ``ConfigStore`` (used in tests or when switching projects)."""
    global _default_store
    if not isinstance(store, ConfigStore):
        raise ConfigError(f"store must be a ConfigStore, got {type(store).__name__}")
    with _default_store_lock:
        _default_store = store
    return store


def load_config(path: str | Path | None = None, *, create: bool = True) -> Config:
    """Read the config (default ``~/.agent/config.yaml``; generates the default config file if missing)."""
    return get_config_store(path).load(create=create)


def save_config(config: Config, path: str | Path | None = None) -> Path:
    """Write the config and return the actual written path."""
    return get_config_store(path).save(config)

