"""Effective configuration: defaults → file → environment → explicit overrides.

``ConfigStore`` only knows how to read/write the file.  ``ConfigService`` is the
single API the CLI, TUI, Web gateway and ``AgentHost`` will use:

* merge all sources with a deterministic precedence;
* resolve ``${ENV_VAR}`` references without ever writing them back;
* validate the merged result;
* track the *source* of every leaf value (default/file/env/cli);
* write changes atomically and notify listeners;
* classify changes into ``live`` / ``new_session`` / ``restart``.

Per-conversation values (``workspace``) are deliberately absent; they live in
:class:`~linteum_agent.config.options.SessionOptions`.
"""

from __future__ import annotations

import copy
import os
import re
import threading
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

from .meta import (
    FILE_ONLY_KEYS,
    FieldSpec,
    apply_scope,
    build_schema,
    coerce_value,
    field_specs,
    get_spec,
)
from .schema import (
    AppConfig,
    ConfigError,
    Issue,
    app_from_dict,
    app_to_dict,
    default_config,
    raise_for_issues,
    validate_config,
)
from .store import ConfigStore

#: Environment variable prefix for structured overrides:
#: ``agent.llm.model`` -> ``LINTEUM_AGENT__LLM__MODEL``.
ENV_PREFIX = "LINTEUM_"

_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_FULL_ENV_REF = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ConfigValue:
    """One effective value with its origin and masking information."""

    key: str
    value: Any
    source: str = "default"        # default | file | env | cli
    secret: bool = False
    set: bool = True
    masked: Optional[str] = None
    raw_value: Any = None

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "value": self.value,
            "source": self.source,
            "secret": self.secret,
            "set": self.set,
            "masked": self.masked,
            "raw_value": self.raw_value,
        }


@dataclass(frozen=True, slots=True)
class ConfigChange:
    key: str
    old: Any
    new: Any
    scope: str = "new_session"

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "old": self.old,
            "new": self.new,
            "scope": self.scope,
        }


@dataclass(frozen=True, slots=True)
class ChangeSet:
    revision: str
    changes: tuple[ConfigChange, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.changes)

    @property
    def keys(self) -> tuple[str, ...]:
        return tuple(change.key for change in self.changes)

    def to_dict(self) -> dict:
        return {
            "revision": self.revision,
            "changes": [change.to_dict() for change in self.changes],
        }


@dataclass(frozen=True, slots=True)
class ApplyResult:
    revision: str
    applied_live: tuple[str, ...] = ()
    requires_new_session: tuple[str, ...] = ()
    requires_restart: tuple[str, ...] = ()
    warnings: tuple[Issue, ...] = ()

    def to_dict(self) -> dict:
        return {
            "revision": self.revision,
            "applied_live": list(self.applied_live),
            "requires_new_session": list(self.requires_new_session),
            "requires_restart": list(self.requires_restart),
            "warnings": [issue.to_dict() for issue in self.warnings],
        }


@dataclass(frozen=True, slots=True)
class EffectiveConfig:
    """The merged runtime view of the configuration."""

    config: AppConfig
    sources: Mapping[str, str]
    revision: str
    issues: tuple[Issue, ...] = ()
    #: Flattened file leaves (pre-env/pre-override) for ``raw_value``.
    raw: Mapping[str, Any] = field(default_factory=dict)

    @property
    def warnings(self) -> tuple[Issue, ...]:
        return tuple(issue for issue in self.issues if issue.severity == "warning")

    def source(self, key: str) -> str:
        if key in self.sources:
            return self.sources[key]
        # Dynamic profile/fallback leaves inherit the composite's source so
        # `config get agent.profiles.fast.model` does not report "default".
        for parent in ("agent.profiles", "agent.fallbacks"):
            if key.startswith(parent + "."):
                return self.sources.get(parent, "default")
        return "default"

    def _raw_for(self, spec: FieldSpec) -> Any:
        """File-level value for non-secret scalar leaves (``${VAR}`` intact)."""
        if spec.secret or spec.type in ("map", "list", "object"):
            return None
        if self.source(spec.key) != "file":
            return None
        raw = self.raw.get(spec.key)
        # YAML can produce dates/binary objects; the gateway response must stay
        # JSON-serialisable, so only pass through JSON scalar types.
        if raw is None or isinstance(raw, (str, int, float, bool)):
            return raw
        return None

    def _value_for(self, spec: FieldSpec, value: Any, *,
                   reveal: bool = False) -> ConfigValue:
        if isinstance(value, (dict, list, tuple)):
            is_set = bool(value)
        else:
            is_set = value is not None and value != ""
        masked = _mask(value) if spec.secret and is_set else None
        shown = value if (reveal or not spec.secret) else None
        if not reveal and spec.type in ("map", "list", "object") and shown is not None:
            shown = _mask_secret_fields(shown)
        return ConfigValue(
            key=spec.key,
            value=shown,
            source=self.source(spec.key),
            secret=spec.secret,
            set=is_set,
            masked=masked,
            raw_value=self._raw_for(spec),
        )

    def get(self, key: str, *, reveal: bool = False) -> ConfigValue:
        spec = get_spec(key, self.config)
        data = app_to_dict(self.config)
        return self._value_for(spec, _get_path(data, key), reveal=reveal)

    def values(self, *, reveal: bool = False) -> dict[str, dict]:
        """Return every schema leaf as ``ConfigValue.to_dict()``."""
        data = app_to_dict(self.config)
        return {
            spec.key: self._value_for(
                spec, _get_path(data, spec.key), reveal=reveal,
            ).to_dict()
            for spec in field_specs(self.config)
        }

    def to_dict(self, *, reveal: bool = False) -> dict:
        data = app_to_dict(self.config)
        return data if reveal else _mask_secret_fields(data)

    def sources_dict(self) -> dict:
        """Source for every leaf, including dynamic profile/fallback keys."""
        data = _flatten(app_to_dict(self.config))
        return {
            key: self.source(key)
            for key in data
            if key != "version"
        }


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _flatten(data: Mapping[str, Any], prefix: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in data.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, Mapping):
            out.update(_flatten(value, path))
        else:
            out[path] = value
    return out


def _get_path(data: Any, key: str) -> Any:
    current = data
    for part in key.split("."):
        if isinstance(current, Mapping):
            if part not in current:
                return None
            current = current[part]
        elif isinstance(current, (list, tuple)) and part.isdigit():
            index = int(part)
            if index >= len(current):
                return None
            current = current[index]
        else:
            return None
    return current


def _set_path(data: dict, key: str, value: Any) -> None:
    parts = key.split(".")
    current: Any = data
    for index, part in enumerate(parts[:-1]):
        next_part = parts[index + 1]
        if isinstance(current, list):
            slot = int(part)
            while len(current) <= slot:
                current.append(None)
            child = current[slot]
        else:
            child = current.get(part)
        if child is None:
            child = [] if next_part.isdigit() else {}
            if isinstance(current, list):
                current[int(part)] = child
            else:
                current[part] = child
        current = child
    last = parts[-1]
    if isinstance(current, list):
        slot = int(last)
        while len(current) <= slot:
            current.append(None)
        current[slot] = value
    else:
        current[last] = value


def _deep_merge(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict:
    result = dict(base)
    for key, value in override.items():
        if isinstance(value, Mapping) and isinstance(result.get(key), Mapping):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _mask(value: Any) -> str:
    text = "" if value is None else str(value)
    if len(text) <= 8:
        return "***"
    return f"{text[:3]}…{text[-4:]}"


_SECRET_LEAVES = frozenset({"api_key", "password", "secret", "token"})


def _is_secret_key(key: str) -> bool:
    return key.rsplit(".", 1)[-1] in _SECRET_LEAVES


def _mask_secret_fields(value: Any) -> Any:
    """Recursively mask secret leaves (including dynamic profile keys)."""
    if isinstance(value, Mapping):
        out = {}
        for key, item in value.items():
            name = str(key)
            if _is_secret_key(name):
                out[name] = _mask(item) if item not in (None, "") else None
            else:
                out[name] = _mask_secret_fields(item)
        return out
    if isinstance(value, (list, tuple)):
        return [_mask_secret_fields(item) for item in value]
    return value


def _clear_unresolved_secrets(value: Any, prefix: str = "") -> None:
    """Turn any remaining ``${VAR}`` secret into ``None`` (never send literals)."""
    if isinstance(value, Mapping):
        for key, item in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            if path in FILE_ONLY_KEYS:
                # File-only maps are literal by design (§4.2); never rewrite
                # their values just because a key looks secret-like.
                continue
            if _is_secret_key(str(key)) and isinstance(item, str) and "${" in item:
                value[key] = None
            else:
                _clear_unresolved_secrets(item, path)
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _clear_unresolved_secrets(item, f"{prefix}[{index}]")


def env_var_for(key: str, prefix: str = ENV_PREFIX) -> str:
    return prefix + key.upper().replace(".", "__")


def _interpolate(
    data: dict,
    *,
    environ: Mapping[str, str],
    specs: Mapping[str, Any],
    issues: list[Issue],
    prefix: str = "",
) -> None:
    """Resolve ``${ENV_VAR}`` in place (memory only; never written back)."""
    for key in list(data):
        path = f"{prefix}.{key}" if prefix else str(key)
        if path in FILE_ONLY_KEYS:
            # File-only maps stay literal (§4.2): their only environment
            # injection path is the explicit ``env:VAR`` secret reference.
            continue
        value = data[key]
        if isinstance(value, Mapping):
            _interpolate(value, environ=environ, specs=specs, issues=issues, prefix=path)
            continue
        if isinstance(value, (list, tuple)):
            for index, item in enumerate(value):
                if isinstance(item, Mapping):
                    _interpolate(item, environ=environ, specs=specs, issues=issues,
                                 prefix=f"{path}[{index}]")
            continue
        if not isinstance(value, str) or "${" not in value:
            continue

        full = _FULL_ENV_REF.match(value)
        if full:
            name = full.group(1)
            if name in environ:
                data[key] = environ[name]
            else:
                spec = specs.get(path)
                if spec is not None and (spec.nullable or spec.secret):
                    data[key] = None
                issues.append(Issue(
                    key=path,
                    code="E_CONFIG_ENV_MISSING",
                    message=f"environment variable {name} is not set",
                    severity="warning",
                    hint=f"export {name}=... or set the value directly",
                ))
            continue

        missing: list[str] = []

        def replace_ref(match: re.Match) -> str:
            name = match.group(1)
            if name in environ:
                return str(environ[name])
            missing.append(name)
            return match.group(0)

        data[key] = _ENV_REF.sub(replace_ref, value)
        for name in missing:
            issues.append(Issue(
                key=path,
                code="E_CONFIG_ENV_MISSING",
                message=f"environment variable {name} is not set",
                severity="warning",
                hint=f"export {name}=...",
            ))


# ---------------------------------------------------------------------------
# ConfigService
# ---------------------------------------------------------------------------


class ConfigService:
    """Merge, validate, persist and broadcast configuration changes."""

    def __init__(
        self,
        store: Optional[ConfigStore] = None,
        *,
        environ: Optional[Mapping[str, str]] = None,
        prefix: str = ENV_PREFIX,
    ) -> None:
        self.store = store if store is not None else ConfigStore()
        self.environ = dict(environ) if environ is not None else dict(os.environ)
        self.prefix = prefix
        self._lock = threading.RLock()
        self._listeners: list[Callable[[ChangeSet], None]] = []
        self._last_config: Optional[AppConfig] = None

    # -- schema -------------------------------------------------------------
    def schema(self) -> dict:
        return build_schema()

    def path(self) -> str:
        return str(self.store.path)

    def revision(self) -> str:
        return self.store.revision()

    def exists(self) -> bool:
        return self.store.exists()

    # -- read ---------------------------------------------------------------
    def effective(
        self,
        overrides: Optional[Mapping[str, Any]] = None,
        *,
        strict: bool = True,
    ) -> EffectiveConfig:
        """Merge every source and return the effective configuration."""
        defaults = app_to_dict(default_config())
        specs = {spec.key: spec for spec in field_specs()}

        merged = dict(defaults)
        sources = {key: "default" for key in _flatten(defaults)}
        sources.pop("version", None)

        raw_file, file_revision = self.store.load_raw_with_revision()
        raw_flat: dict[str, Any] = {}
        if raw_file:
            merged = _deep_merge(merged, raw_file)
            raw_flat = _flatten(raw_file)
            for key in raw_flat:
                if key != "version" and key in specs:
                    sources[key] = "file"
            for key, spec in specs.items():
                if spec.type in ("map", "list") and (
                    key in raw_flat
                    or any(child.startswith(key + ".") for child in raw_flat)
                ):
                    sources[key] = "file"

        for key, spec in specs.items():
            env_name = env_var_for(key, self.prefix)
            if env_name in self.environ:
                value = coerce_value(spec, self.environ[env_name])
                _set_path(merged, key, value)
                sources[key] = "env"

        if overrides:
            flat_overrides: dict[str, Any] = {}
            for key, raw_value in dict(overrides).items():
                if key in specs and specs[key].type in ("map", "list"):
                    flat_overrides[key] = raw_value
                else:
                    flat_overrides.update(_flatten({key: raw_value}))
            for key, raw_value in flat_overrides.items():
                spec = specs.get(key) or get_spec(key)
                _set_path(merged, key, coerce_value(spec, raw_value))
                sources[key] = "cli"
                for parent, parent_spec in specs.items():
                    if (parent_spec.type in ("map", "list")
                            and key.startswith(parent + ".")):
                        sources[parent] = "cli"

        issues: list[Issue] = []
        _interpolate(merged, environ=self.environ, specs=specs, issues=issues)
        # A partially-resolved secret must never reach a provider as a literal
        # "${...}" string.
        _clear_unresolved_secrets(merged)

        config = app_from_dict(merged)
        issues.extend(validate_config(config, effective=True))
        if strict:
            raise_for_issues(issues)

        with self._lock:
            self._last_config = config
        return EffectiveConfig(
            config=config,
            sources={key: sources.get(key, "default") for key in specs},
            revision=file_revision,
            issues=tuple(issues),
            raw=raw_flat,
        )

    def get(self, key: str, *, reveal: bool = False) -> ConfigValue:
        return self.effective(strict=False).get(key, reveal=reveal)

    def to_dict(self, *, reveal: bool = False) -> dict:
        return self.effective(strict=False).to_dict(reveal=reveal)

    def sources(self) -> dict:
        return self.effective(strict=False).sources_dict()

    # -- write --------------------------------------------------------------
    def set(
        self,
        key: str,
        value: Any,
        *,
        expected_revision: Optional[str] = None,
        persist: bool = True,
    ) -> ChangeSet:
        """Validate and persist one dotted key; return the resulting diff."""
        return self.update(
            {key: value},
            expected_revision=expected_revision,
            persist=persist,
        )

    def unset(
        self,
        key: str,
        *,
        expected_revision: Optional[str] = None,
        persist: bool = True,
    ) -> ChangeSet:
        """Reset a key to its schema default."""
        return self.unset_many(
            [key],
            expected_revision=expected_revision,
            persist=persist,
        )

    def update(
        self,
        changes: Mapping[str, Any],
        *,
        expected_revision: Optional[str] = None,
        persist: bool = True,
    ) -> ChangeSet:
        """Validate and persist a batch of dotted keys atomically."""
        if not changes:
            return ChangeSet(revision=self.store.revision(), changes=())

        resolved: list[tuple[str, Any]] = []
        for key, value in changes.items():
            spec = get_spec(key)
            resolved.append((key, coerce_value(spec, value)))
        _reject_overlapping_keys(key for key, _ in resolved)

        with self._lock:
            # Load the raw file instead of ``store.load`` so a batch that
            # repairs a semantic error (bad enum/range/cross-field) is not
            # rejected by validating the *old* config first.  The new config
            # is still validated before ``save`` touches the file.
            raw = self.store.load_raw()
            old = app_from_dict(raw) if raw else default_config()
            data = app_to_dict(old)
            for key, coerced in resolved:
                _set_path(data, key, coerced)
            new = app_from_dict(data)
            raise_for_issues(validate_config(new))

            diff = _diff(old, new)
            if not diff:
                current = self.store.revision()
                if (persist and expected_revision is not None
                        and expected_revision != current):
                    raise ConfigError(
                        "configuration changed since it was read",
                        code="E_CONFIG_CONFLICT",
                        hint="reload the configuration and re-apply",
                    )
                return ChangeSet(revision=current, changes=())

            if persist:
                revision = self.store.save(
                    new, expected_revision=expected_revision,
                )
                self._last_config = new
            else:
                revision = self.store.revision()
            result = ChangeSet(revision=revision, changes=diff)

        if persist:
            self._notify(result)
        return result

    def unset_many(
        self,
        keys: Sequence[str],
        *,
        expected_revision: Optional[str] = None,
        persist: bool = True,
    ) -> ChangeSet:
        """Reset keys to their schema defaults in one transaction."""
        defaults = {
            key: copy.deepcopy(get_spec(key).default) for key in keys
        }
        return self.update(
            defaults,
            expected_revision=expected_revision,
            persist=persist,
        )

    def apply(self, changes: Optional[ChangeSet] = None) -> ApplyResult:
        """Classify a change set into live / new_session / restart."""
        if changes is None:
            changes = self.reload()
        live: list[str] = []
        new_session: list[str] = []
        restart: list[str] = []
        for change in changes.changes:
            if change.scope == "live":
                live.append(change.key)
            elif change.scope == "restart":
                restart.append(change.key)
            else:
                new_session.append(change.key)
        effective = self.effective(strict=False)
        return ApplyResult(
            revision=changes.revision,
            applied_live=tuple(live),
            requires_new_session=tuple(new_session),
            requires_restart=tuple(restart),
            warnings=effective.warnings,
        )

    def reload(self) -> ChangeSet:
        """Re-read the file and broadcast the diff against the last view."""
        with self._lock:
            old = self._last_config or self.store.load(create=False)
            new = self.store.load(create=False)
            raise_for_issues(validate_config(new))
            self._last_config = new
            changes = _diff(old, new)
            change_set = ChangeSet(revision=self.store.revision(), changes=changes)
        self._notify(change_set)
        return change_set

    def validate(self, *, effective: bool = False,
                 strict: bool = False) -> list[Issue]:
        try:
            raw = self.store.load_raw()
            config = app_from_dict(raw) if raw else default_config()
            issues = validate_config(config, effective=effective)
        except ConfigError as exc:
            issues = [Issue(
                key=exc.key or "",
                code=exc.code,
                message=str(exc),
                severity="error",
                hint=exc.hint,
            )]
        if strict:
            issues = [
                replace(issue, severity="error") if issue.severity == "warning" else issue
                for issue in issues
            ]
        return issues

    def ensure(self) -> AppConfig:
        """Create the default file if needed and return the file config."""
        return self.store.ensure()

    # -- listeners ----------------------------------------------------------
    def subscribe(self, listener: Callable[[ChangeSet], None]) -> Callable[[], None]:
        with self._lock:
            self._listeners.append(listener)

        def unsubscribe() -> None:
            with self._lock:
                try:
                    self._listeners.remove(listener)
                except ValueError:
                    pass

        return unsubscribe

    def _notify(self, changes: ChangeSet) -> None:
        with self._lock:
            listeners = list(self._listeners)
        for listener in listeners:
            try:
                listener(changes)
            except Exception:  # noqa: BLE001 - a listener must not break the write
                continue


def _reject_overlapping_keys(keys: Iterable[str]) -> None:
    """Reject a batch that sets both a parent object and one of its children."""
    ordered = sorted(set(keys))
    for parent, child in zip(ordered, ordered[1:]):
        if child.startswith(parent + "."):
            raise ConfigError(
                f"configuration keys overlap: {parent!r} and {child!r}",
                key=parent,
                code="E_CONFIG_KEY_OVERLAP",
                hint="set either the parent object or its child fields, not both",
            )


def _diff(old: AppConfig, new: AppConfig) -> tuple[ConfigChange, ...]:
    old_data = app_to_dict(old)
    new_data = app_to_dict(new)
    old_flat = _flatten(old_data)
    new_flat = _flatten(new_data)
    changes: list[ConfigChange] = []
    for key in sorted(set(old_flat) | set(new_flat)):
        if key == "version":
            continue
        before, after = old_flat.get(key), new_flat.get(key)
        if before != after:
            change = ConfigChange(
                key=key,
                old=before,
                new=after,
                scope=apply_scope(key),
            )
            if key in ("agent.profiles", "agent.fallbacks"):
                # Composite maps/lists arrive as a single flat leaf (lists are
                # never recursed into), so their secret children must be masked
                # here.  Never serialize a raw api_key in a diff.
                change = replace(
                    change,
                    old=_mask_secret_fields(before),
                    new=_mask_secret_fields(after),
                )
            elif _is_secret_key(key):
                change = replace(
                    change,
                    old=_mask(before) if before not in (None, "") else None,
                    new=_mask(after) if after not in (None, "") else None,
                )
            changes.append(change)

    # Collapse dynamic profile leaves into one stable composite change so
    # listeners never have to understand arbitrary profile names.
    parents = {
        parent
        for change in changes
        for parent in ("agent.profiles", "agent.fallbacks")
        if change.key.startswith(parent + ".")
    }
    if not parents:
        return tuple(changes)

    result = [
        change for change in changes
        if not any(change.key.startswith(parent + ".") for parent in parents)
    ]
    existing = {change.key for change in result}
    for parent in sorted(parents):
        if parent in existing:
            continue
        result.append(ConfigChange(
            key=parent,
            old=_mask_secret_fields(_get_path(old_data, parent)),
            new=_mask_secret_fields(_get_path(new_data, parent)),
            scope=apply_scope(parent),
        ))
    result.sort(key=lambda change: change.key)
    return tuple(result)


__all__ = [
    "ApplyResult",
    "ChangeSet",
    "ConfigChange",
    "ConfigService",
    "ConfigValue",
    "ENV_PREFIX",
    "EffectiveConfig",
    "env_var_for",
]
