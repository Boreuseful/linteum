"""Structured errors for the plugin layer (§9.3).

The plugin layer is host-side, so these errors never cross the bus.  The host
converts them into ``system.plugin.failed`` payloads whose ``error_info`` has
``source="plugin"``; the stable ``code`` strings match the ``E_PLUGIN_*`` names
in :class:`linteum_agent.bus.errors.ErrorCode`.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

from ...bus.error_info import ErrorSource, error_info


class PluginError(Exception):
    """Base class for every plugin failure.

    ``key``/``hint``/``context`` mirror :class:`ModeError` and
    :class:`~linteum_agent.config.schema.ConfigError` so the host can render a
    precise diagnostic without guessing.  ``detail`` keeps the original
    exception text (never a traceback) for ``error_info``.
    """

    code: str = "E_PLUGIN_ERROR"
    retryable: bool = False

    def __init__(
        self,
        message: str = "",
        *,
        key: Optional[str] = None,
        hint: str = "",
        context: Optional[Mapping[str, Any]] = None,
        detail: str = "",
        exc_type: str = "",
    ) -> None:
        super().__init__(message or self.__class__.__name__)
        self.key = key
        self.hint = hint
        self.context = dict(context or {})
        self.detail = detail or str(self)
        self.exc_type = exc_type

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "error": str(self),
            "key": self.key,
            "hint": self.hint,
            "context": dict(self.context),
        }

    def to_error_info(self, *, location: str = "") -> dict:
        """Structured detail for ``system.plugin.failed`` (§9.3)."""
        return error_info(
            self.code,
            source=ErrorSource.PLUGIN.value,
            location=location,
            message=str(self),
            exc_type=self.exc_type or None,
            detail=self.detail or None,
            retryable=self.retryable,
            enters_history=False,
            context=self.context,
        ).to_dict()


class PluginManifestError(PluginError):
    """YAML / field / type / unknown-field error."""

    code = "E_PLUGIN_MANIFEST"


class PluginUnsupported(PluginError):
    """``api_version`` / ``runtime`` / capability is not supported in v0.1."""

    code = "E_PLUGIN_UNSUPPORTED"


class PluginPathError(PluginError):
    """A declared path escaped the plugin root, or is a symlink/irregular file."""

    code = "E_PLUGIN_PATH"


class PluginNotFound(PluginError):
    """The id was not discovered or is not loaded."""

    code = "E_PLUGIN_NOT_FOUND"
    retryable = True


class PluginDisabled(PluginError):
    """The id is not in ``agent.plugins.allow``."""

    code = "E_PLUGIN_DISABLED"


class PluginLimit(PluginError):
    """A plugin / tool / file count or size limit was exceeded."""

    code = "E_PLUGIN_LIMIT"


class PluginTimeout(PluginError):
    """Import, setup or cleanup exceeded its budget."""

    code = "E_PLUGIN_TIMEOUT"
    retryable = True


class PluginConflict(PluginError):
    """A tool name conflicts with a reserved name or another plugin."""

    code = "E_PLUGIN_CONFLICT"


class PluginToolUndeclared(PluginError):
    """``setup`` provided a tool the manifest did not declare."""

    code = "E_PLUGIN_TOOL_UNDECLARED"


class PluginToolMissing(PluginError):
    """The manifest declared a tool that ``setup`` did not provide."""

    code = "E_PLUGIN_TOOL_MISSING"


class PluginLoadError(PluginError):
    """Any other import / setup failure."""

    code = "E_PLUGIN_LOAD"
    retryable = True


class PluginBusy(PluginError):
    """A concurrent lifecycle transition owns this plugin."""

    code = "E_PLUGIN_BUSY"
    retryable = True


class PluginManageDisabled(PluginError):
    """``gateway.plugins.manage`` is off; clients may only read (§11.5.8)."""

    code = "E_PLUGIN_MANAGE_DISABLED"


class PluginConfigError(PluginError):
    """The configuration file is unreadable, env-overridden, or unwritable."""

    code = "E_PLUGIN_CONFIG"
    retryable = True


def as_plugin_error(exc: BaseException) -> PluginError:
    """Wrap an arbitrary exception without losing its type/details."""
    if isinstance(exc, PluginError):
        return exc
    return PluginLoadError(
        f"{type(exc).__name__}: {exc}",
        detail=str(exc),
        exc_type=type(exc).__name__,
    )


__all__ = [
    "PluginBusy",
    "PluginConfigError",
    "PluginConflict",
    "PluginDisabled",
    "PluginError",
    "PluginManageDisabled",
    "PluginLimit",
    "PluginLoadError",
    "PluginManifestError",
    "PluginNotFound",
    "PluginPathError",
    "PluginTimeout",
    "PluginToolMissing",
    "PluginToolUndeclared",
    "PluginUnsupported",
    "as_plugin_error",
]
