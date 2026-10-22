"""Filesystem locations for the config/state directory.

Kept dependency-free so both the schema defaults and the YAML store can use it
without an import cycle::

    LINTEUM_HOME   -> ~/.linteum (config.yaml, state.db, artifacts/, logs/)
    LINTEUM_CONFIG -> override the exact config file path
"""

from __future__ import annotations

import os
from pathlib import Path

#: User state directory name under ``$HOME``.
AGENT_DIRNAME = ".linteum"
#: Default config file name.
DEFAULT_CONFIG_FILENAME = "config.yaml"
#: Environment variable that overrides the exact config file path.
CONFIG_PATH_ENV = "LINTEUM_CONFIG"
#: Environment variable that overrides the user state directory.
HOME_ENV = "LINTEUM_HOME"


def linteum_home() -> Path:
    """Return the user state directory (``~/.linteum`` by default)."""
    override = os.environ.get(HOME_ENV)
    if override and override.strip():
        return Path(override).expanduser()
    return Path.home() / AGENT_DIRNAME


def default_config_path() -> Path:
    """Return the config path (``LINTEUM_CONFIG`` wins over ``~/.linteum``)."""
    override = os.environ.get(CONFIG_PATH_ENV)
    if override and override.strip():
        return Path(override).expanduser()
    return linteum_home() / DEFAULT_CONFIG_FILENAME


__all__ = [
    "AGENT_DIRNAME",
    "CONFIG_PATH_ENV",
    "DEFAULT_CONFIG_FILENAME",
    "HOME_ENV",
    "default_config_path",
    "linteum_home",
]
