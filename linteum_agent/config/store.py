"""Read/write the semi-permanent YAML configuration file.

Default location: ``~/.linteum/config.yaml`` (override with ``LINTEUM_CONFIG``).
The directory intentionally uses the ``.linteum`` name; this is a fresh config
layer and does **not** read or migrate the legacy ``~/.agent`` file.

Write semantics:

* validate first, then write;
* write a temp file in the same directory, ``fsync`` it and ``os.replace`` it
  (a crash never leaves a half-written config);
* tighten permissions to ``0600`` when the file contains a literal API key;
* keep a small number of timestamped backups;
* detect concurrent writers with ``expected_revision`` (sha256 of the file);
* serialise writers with an advisory file lock (``fcntl`` where available).
"""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Mapping, Optional

import yaml

from .paths import (
    AGENT_DIRNAME,
    CONFIG_PATH_ENV,
    DEFAULT_CONFIG_FILENAME,
    HOME_ENV,
    default_config_path,
    linteum_home,
)
from .schema import (
    AppConfig,
    ConfigError,
    app_from_dict,
    app_to_dict,
    default_config,
    raise_for_issues,
    validate_config,
)

CONFIG_HEADER = """\
# linteum_agent configuration (YAML)
#
# This file stores SEMI-PERMANENT settings only: model/credentials, gateway
# limits, storage paths, logging and UI preferences.
#
# The workspace is NOT configured here. It is supplied per conversation when a
# session is created (session.create {workspace}), so every conversation can
# use a different directory.
#
# Multiple providers/models: agent.profiles + agent.default_profile, with an
# optional agent.fallbacks chain for automatic failover.
#
# agent.llm.api_key may be a literal value (the file is then chmod 0600) or a
# ${ENV_VAR} reference. Without it, the provider's default key variable is used
# (OPENAI_API_KEY / DEEPSEEK_API_KEY / ANTHROPIC_API_KEY / ...).
#
# Edit this file directly, or use:
#   linteum config list / get / set / unset / edit / validate
# `linteum config edit` validates the file on save. Map/list values accept JSON,
# e.g. linteum config set agent.profiles '{"fast": {...}}'.
"""


def to_yaml(config: AppConfig) -> str:
    """Serialize a config into YAML text (stable field order, UTF-8)."""
    try:
        return yaml.safe_dump(
            app_to_dict(config),
            allow_unicode=True,
            sort_keys=False,
            default_flow_style=False,
        )
    except yaml.YAMLError as exc:
        raise ConfigError(f"cannot serialize config: {exc}", key="",
                          code="E_CONFIG_SERIALIZE") from exc


def default_config_yaml(config: Optional[AppConfig] = None) -> str:
    """Return the comment header + serialized default config."""
    return CONFIG_HEADER + to_yaml(config if config is not None else default_config())


_SECRET_LEAVES = frozenset({"api_key", "password", "secret", "token"})


def _has_literal_secret(config: AppConfig) -> bool:
    """True when any profile/fallback contains a literal (non-``${VAR}``) secret."""

    def walk(value: Any) -> bool:
        if isinstance(value, Mapping):
            for key, item in value.items():
                name = str(key)
                if name.rsplit(".", 1)[-1] in _SECRET_LEAVES:
                    if isinstance(item, str) and item and not (
                        item.startswith("${") and item.endswith("}")
                    ):
                        return True
                if walk(item):
                    return True
            return False
        if isinstance(value, (list, tuple)):
            return any(walk(item) for item in value)
        return False

    return walk(app_to_dict(config))


@contextmanager
def _file_lock(path: Path) -> Iterator[None]:
    """Best-effort advisory lock; unavailable platforms degrade to a no-op."""
    lock_path = path.with_name(path.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(lock_path, "a+", encoding="utf-8")
    locked = False
    try:
        try:
            import fcntl  # type: ignore[import-not-found]

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            locked = True
        except (ImportError, OSError):
            locked = False
        yield
    finally:
        if locked:
            try:
                import fcntl  # type: ignore[import-not-found]

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except (ImportError, OSError):
                pass
        handle.close()


def _atomic_write(path: Path, text: str, *, secret: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        try:
            os.chmod(path, 0o600 if secret else 0o644)
        except OSError:
            pass
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


class ConfigStore:
    """YAML file read/write with validation, backups and conflict detection."""

    def __init__(
        self,
        path: str | Path | None = None,
        *,
        backup_count: int = 5,
    ) -> None:
        self.path = Path(path).expanduser() if path is not None else default_config_path()
        self.backup_count = max(0, int(backup_count))

    # -- reads --------------------------------------------------------------
    def exists(self) -> bool:
        return self.path.is_file()

    def revision(self) -> str:
        """Content hash used for optimistic concurrency; ``""`` when missing."""
        try:
            data = self.path.read_bytes()
        except OSError:
            return ""
        return hashlib.sha256(data).hexdigest()

    def load_raw(self) -> dict:
        """Return the raw YAML mapping (``{}`` when the file is missing/empty)."""
        try:
            text = self.path.read_text(encoding="utf-8")
        except OSError as exc:
            if isinstance(exc, FileNotFoundError):
                return {}
            raise ConfigError(f"cannot read config file {self.path}: {exc}",
                              key="", code="E_CONFIG_IO") from exc
        if not text.strip():
            return {}
        try:
            raw = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise ConfigError(f"invalid YAML in {self.path}: {exc}",
                              key="", code="E_CONFIG_PARSE") from exc
        if raw is None:
            return {}
        if not isinstance(raw, Mapping):
            raise ConfigError(f"config root in {self.path} must be a mapping",
                              key="", code="E_CONFIG_PARSE")
        return dict(raw)

    def load(self, *, create: bool = False) -> AppConfig:
        """Load and validate the config.

        When the file is missing:

        * ``create=True`` writes the default file first, then returns it;
        * ``create=False`` returns the in-memory defaults without writing.
        """
        if not self.exists():
            config = default_config()
            if create:
                self.save(config)
            return config

        config = app_from_dict(self.load_raw())
        issues = validate_config(config)
        raise_for_issues(issues)
        return config

    # -- writes -------------------------------------------------------------
    def save(
        self,
        config: AppConfig,
        *,
        expected_revision: Optional[str] = None,
    ) -> str:
        """Validate, then atomically replace the file; return the new revision."""
        issues = validate_config(config)
        raise_for_issues(issues)

        with _file_lock(self.path):
            current = self.revision()
            if expected_revision is not None and expected_revision != current:
                raise ConfigError(
                    "config changed since it was read; reload and retry",
                    key="", code="E_CONFIG_CONFLICT",
                    hint=f"current revision is {current or '<missing>'}",
                )
            if self.path.is_file():
                self.backup()
            _atomic_write(self.path, default_config_yaml(config),
                          secret=_has_literal_secret(config))
            return self.revision()

    def backup(self) -> Optional[Path]:
        """Copy the current file to ``config.yaml.bak.<timestamp>``."""
        if not self.path.is_file():
            return None
        stamp = time.strftime("%Y%m%d-%H%M%S")
        target = self.path.with_name(f"{self.path.name}.bak.{stamp}")
        suffix = 1
        while target.exists():
            target = self.path.with_name(f"{self.path.name}.bak.{stamp}.{suffix}")
            suffix += 1
        try:
            shutil.copy2(self.path, target)
        except OSError:
            return None
        self._prune_backups()
        return target

    def _prune_backups(self) -> None:
        if self.backup_count <= 0:
            backups = sorted(self.path.parent.glob(f"{self.path.name}.bak.*"))
            for item in backups:
                try:
                    item.unlink()
                except OSError:
                    pass
            return
        backups = sorted(
            self.path.parent.glob(f"{self.path.name}.bak.*"),
            key=lambda item: item.stat().st_mtime if item.exists() else 0.0,
        )
        for item in backups[: max(0, len(backups) - self.backup_count)]:
            try:
                item.unlink()
            except OSError:
                pass

    # -- convenience --------------------------------------------------------
    def ensure(self) -> AppConfig:
        """Create the default file if needed and return the loaded config."""
        return self.load(create=True)


__all__ = [
    "AGENT_DIRNAME",
    "CONFIG_HEADER",
    "CONFIG_PATH_ENV",
    "ConfigStore",
    "DEFAULT_CONFIG_FILENAME",
    "HOME_ENV",
    "default_config_path",
    "default_config_yaml",
    "linteum_home",
    "to_yaml",
]
