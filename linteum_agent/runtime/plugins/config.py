"""Pure per-plugin config resolution and secret injection (§4).

This module is deliberately **I/O-free**: it never reads environment variables
itself, never touches a file and never imports plugin code.  The host passes the
file mapping and the environment in, so discovery/validation can run against
untrusted manifests and replay never reaches the network or the process env.

The supported ``config_schema`` subset mirrors design §4.1::

    type, properties, required, default, enum, minimum, maximum,
    minLength, maxLength, items, additionalProperties, description, title

Any other keyword is a manifest error (``E_PLUGIN_MANIFEST``).  Value failures
at load time are ``E_PLUGIN_CONFIG`` and carry a key path and a short hint;
messages never contain configured values or secret values (§4.5).
"""

from __future__ import annotations

import math
from types import MappingProxyType
from typing import Any, Mapping, Optional, Type

from .errors import PluginConfigError, PluginError, PluginManifestError
from .model import PluginManifest

#: JSON Schema keywords accepted in ``config_schema`` (§4.1).
_SCHEMA_KEYWORDS: frozenset[str] = frozenset({
    "type",
    "properties",
    "required",
    "default",
    "enum",
    "minimum",
    "maximum",
    "minLength",
    "maxLength",
    "items",
    "additionalProperties",
    "description",
    "title",
})

#: Types accepted by the subset.
_SCHEMA_TYPES: frozenset[str] = frozenset({
    "object", "string", "integer", "number", "boolean", "array",
})

#: The two error classes this module raises; type/range checks are shared.
_ErrorType = Type[PluginError]


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _child_path(path: str, key: str) -> str:
    return f"{path}.{key}" if path else str(key)


def _index_path(path: str, index: int) -> str:
    return f"{path}[{index}]" if path else f"[{index}]"


def _not_json(path: str, error_cls: _ErrorType) -> PluginError:
    return error_cls(
        f"config value for {path!r} is not JSON",
        key=path,
        hint="use strings, numbers, booleans, null, lists or mappings",
    )


def _recursive(path: str, error_cls: _ErrorType) -> PluginError:
    return error_cls(
        f"config value for {path!r} is recursive (not JSON)",
        key=path,
        hint="remove the self-reference; config values must be a finite tree",
    )


def _require_json(
    value: Any,
    path: str,
    error_cls: _ErrorType,
    active: Optional[set[int]] = None,
) -> None:
    """Reject non-JSON values (tuples, sets, bytes, NaN, non-string keys).

    ``active`` holds the ``id()``s of containers on the current descent path,
    so YAML-anchored cycles (``default: &d [*d]``) surface as the caller's
    structured error instead of ``RecursionError``.  Markers are removed on the
    way out, so shared (non-cyclic) containers are still accepted.
    """
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise _not_json(path, error_cls)
        return
    if isinstance(value, (list, Mapping)):
        if active is None:
            active = set()
        marker = id(value)
        if marker in active:
            raise _recursive(path, error_cls)
        active.add(marker)
        try:
            if isinstance(value, list):
                for index, item in enumerate(value):
                    _require_json(
                        item, _index_path(path, index), error_cls, active,
                    )
            else:
                for key, item in value.items():
                    if not isinstance(key, str):
                        raise _not_json(path, error_cls)
                    _require_json(
                        item, _child_path(path, key), error_cls, active,
                    )
        finally:
            active.discard(marker)
        return
    raise _not_json(path, error_cls)


def _copy_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _copy_json(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_copy_json(item) for item in value]
    return value


def _json_equal(left: Any, right: Any) -> bool:
    # JSON distinguishes ``true`` from ``1`` even though Python does not, at
    # any nesting depth (so ``[1]`` and ``[true]`` are different enum values).
    if isinstance(left, bool) != isinstance(right, bool):
        return False
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(
            _json_equal(item_left, item_right)
            for item_left, item_right in zip(left, right)
        )
    if isinstance(left, Mapping) and isinstance(right, Mapping):
        return left.keys() == right.keys() and all(
            _json_equal(left[key], right[key]) for key in left
        )
    return left == right


def _matches_type(kind: str, value: Any) -> bool:
    if kind == "object":
        return isinstance(value, Mapping)
    if kind == "array":
        return isinstance(value, list)
    if kind == "string":
        return isinstance(value, str)
    if kind == "boolean":
        return isinstance(value, bool)
    if kind == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if kind == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return False  # pragma: no cover - schema validation rejects unknown types


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


# ---------------------------------------------------------------------------
# Schema validation (E_PLUGIN_MANIFEST)
# ---------------------------------------------------------------------------

def _manifest_error(message: str, *, key: str, hint: str = "") -> PluginManifestError:
    return PluginManifestError(message, key=key, hint=hint)


def _validate_schema(
    schema: Any,
    *,
    path: str,
    root: bool = False,
    active: Optional[set[int]] = None,
) -> None:
    """Validate one schema node; reject cyclic (YAML-anchored) schemas."""
    if not isinstance(schema, Mapping):
        raise _manifest_error(f"{path} must be a mapping", key=path)
    if active is None:
        active = set()
    marker = id(schema)
    if marker in active:
        raise _manifest_error(
            f"{path} is recursive",
            key=path,
            hint="config_schema must be a finite JSON tree",
        )
    active.add(marker)
    try:
        _validate_schema_node(schema, path=path, root=root, active=active)
    finally:
        active.discard(marker)


def _validate_schema_node(
    schema: Mapping,
    *,
    path: str,
    root: bool,
    active: set[int],
) -> None:
    for key in schema:
        if not isinstance(key, str) or key not in _SCHEMA_KEYWORDS:
            raise _manifest_error(
                f"unsupported config_schema keyword {key!r} at {path!r}",
                key=_child_path(path, str(key)),
                hint="remove it or use the supported JSON Schema subset",
            )

    if "type" in schema:
        kind = schema["type"]
        if not isinstance(kind, str) or kind not in _SCHEMA_TYPES:
            raise _manifest_error(
                f"{path}.type must be one of {sorted(_SCHEMA_TYPES)}",
                key=f"{path}.type",
            )
    if root and schema.get("type") != "object":
        raise _manifest_error(
            "config_schema.type must be 'object'",
            key="config_schema.type",
        )

    if "properties" in schema:
        properties = schema["properties"]
        if not isinstance(properties, Mapping):
            raise _manifest_error(
                f"{path}.properties must be a mapping", key=f"{path}.properties",
            )
        for name, subschema in properties.items():
            if not isinstance(name, str):
                raise _manifest_error(
                    f"{path}.properties keys must be strings",
                    key=f"{path}.properties",
                )
            _validate_schema(
                subschema, path=f"{path}.properties.{name}", active=active,
            )

    if "required" in schema:
        required = schema["required"]
        if (
            not isinstance(required, (list, tuple))
            or any(not isinstance(name, str) for name in required)
        ):
            raise _manifest_error(
                f"{path}.required must be a list of strings",
                key=f"{path}.required",
            )
        if len(set(required)) != len(required):
            raise _manifest_error(
                f"{path}.required contains duplicate names",
                key=f"{path}.required",
            )

    if "enum" in schema:
        enum = schema["enum"]
        if not isinstance(enum, (list, tuple)):
            raise _manifest_error(
                f"{path}.enum must be a list", key=f"{path}.enum",
            )
        for value in enum:
            _require_json(value, f"{path}.enum", PluginManifestError)

    for keyword in ("minimum", "maximum"):
        if keyword in schema:
            value = schema[keyword]
            if not _is_number(value) or not math.isfinite(value):
                raise _manifest_error(
                    f"{path}.{keyword} must be a finite number",
                    key=f"{path}.{keyword}",
                )
    if (
        "minimum" in schema and "maximum" in schema
        and schema["minimum"] > schema["maximum"]
    ):
        raise _manifest_error(
            f"{path}.minimum must not exceed maximum",
            key=f"{path}.minimum",
        )

    for keyword in ("minLength", "maxLength"):
        if keyword in schema:
            value = schema[keyword]
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise _manifest_error(
                    f"{path}.{keyword} must be a non-negative integer",
                    key=f"{path}.{keyword}",
                )
    if (
        "minLength" in schema and "maxLength" in schema
        and schema["minLength"] > schema["maxLength"]
    ):
        raise _manifest_error(
            f"{path}.minLength must not exceed maxLength",
            key=f"{path}.minLength",
        )

    if "items" in schema:
        _validate_schema(schema["items"], path=f"{path}.items", active=active)

    if "additionalProperties" in schema:
        extra = schema["additionalProperties"]
        if not isinstance(extra, bool):
            _validate_schema(
                extra, path=f"{path}.additionalProperties", active=active,
            )

    for keyword in ("description", "title"):
        if keyword in schema and not isinstance(schema[keyword], str):
            raise _manifest_error(
                f"{path}.{keyword} must be a string", key=f"{path}.{keyword}",
            )

    # Validate a declared default with exactly the value checks below, after
    # the keyword forms are known-good.  This also fills nested defaults so a
    # partial object default is judged by what the plugin would actually see.
    if "default" in schema:
        _resolve_value(
            schema,
            schema["default"],
            f"{path}.default",
            PluginManifestError,
            root=root,
        )


def validate_config_schema(schema: object) -> dict:
    """Validate and return a ``config_schema`` mapping (no code execution).

    Raises :class:`PluginManifestError` for a non-mapping schema, a non-object
    root, an unsupported keyword/type, or a default that is not JSON / does not
    satisfy its own schema.
    """
    if not isinstance(schema, Mapping):
        raise _manifest_error(
            "config_schema must be a mapping", key="config_schema",
        )
    _validate_schema(schema, path="config_schema", root=True)
    return dict(schema)


# ---------------------------------------------------------------------------
# Value resolution and validation
# ---------------------------------------------------------------------------

def _raise_type(path: str, kind: str, error_cls: _ErrorType) -> None:
    raise error_cls(
        f"config value for {path!r} must be of type {kind}",
        key=path,
        hint=f"use a JSON {kind}",
    )


def _check_enum(
    schema: Mapping, value: Any, path: str, error_cls: _ErrorType,
) -> None:
    enum = schema.get("enum")
    if enum is not None and not any(_json_equal(value, item) for item in enum):
        raise error_cls(
            f"config value for {path!r} is not one of the allowed values",
            key=path,
            hint="use one of the values declared in config_schema.enum",
        )


def _check_range(
    schema: Mapping, value: Any, path: str, error_cls: _ErrorType,
) -> None:
    if _is_number(value):
        minimum = schema.get("minimum")
        if minimum is not None and value < minimum:
            raise error_cls(
                f"config value for {path!r} must be >= {minimum}",
                key=path, hint="use a value within the declared range",
            )
        maximum = schema.get("maximum")
        if maximum is not None and value > maximum:
            raise error_cls(
                f"config value for {path!r} must be <= {maximum}",
                key=path, hint="use a value within the declared range",
            )
    if isinstance(value, str):
        min_length = schema.get("minLength")
        if min_length is not None and len(value) < min_length:
            raise error_cls(
                f"config value for {path!r} must have at least "
                f"{min_length} characters",
                key=path, hint="use a longer string",
            )
        max_length = schema.get("maxLength")
        if max_length is not None and len(value) > max_length:
            raise error_cls(
                f"config value for {path!r} must have at most "
                f"{max_length} characters",
                key=path, hint="use a shorter string",
            )


def _resolve_object(
    schema: Mapping,
    source: Mapping,
    path: str,
    error_cls: _ErrorType,
    *,
    root: bool = False,
) -> dict:
    properties = schema.get("properties") or {}
    extra = schema.get("additionalProperties")
    if extra is None:
        # Root typos must fail (design §4.1); nested objects follow JSON
        # Schema and allow unspecified keys unless declared otherwise.
        extra = False if root else True

    result: dict = {}
    for key, value in source.items():
        if not isinstance(key, str):
            raise error_cls(
                f"config key {key!r} under {path!r} must be a string",
                key=path, hint="use JSON object keys",
            )
        child = _child_path(path, key)
        if key in properties:
            result[key] = _resolve_value(
                properties[key], value, child, error_cls,
            )
        elif extra is False:
            raise error_cls(
                f"unknown config key {child!r}",
                key=child,
                hint="remove it or declare it in the plugin's config_schema",
            )
        elif extra is True:
            _require_json(value, child, error_cls)
            result[key] = _copy_json(value)
        else:
            result[key] = _resolve_value(extra, value, child, error_cls)

    # Defaults are applied depth-first: a missing property's default is itself
    # resolved, so nested object defaults gain their own nested defaults.
    for key, subschema in properties.items():
        if key not in result and "default" in subschema:
            result[key] = _resolve_value(
                subschema, subschema["default"], _child_path(path, key),
                error_cls,
            )

    for name in schema.get("required") or ():
        if name not in result:
            child = _child_path(path, name)
            raise error_cls(
                f"missing required config key {child!r}",
                key=child,
                hint="set it under agent.plugins.config",
            )
    return result


def _resolve_value(
    schema: Mapping,
    value: Any,
    path: str,
    error_cls: _ErrorType,
    *,
    root: bool = False,
) -> Any:
    _require_json(value, path, error_cls)
    _check_enum(schema, value, path, error_cls)

    kind = schema.get("type")
    is_object = kind == "object" or (
        kind is None and isinstance(value, Mapping)
        and ("properties" in schema or "additionalProperties" in schema)
    )
    is_array = kind == "array" or (
        kind is None and isinstance(value, list) and "items" in schema
    )

    if is_object:
        if not isinstance(value, Mapping):
            _raise_type(path, "object", error_cls)
        return _resolve_object(schema, value, path, error_cls, root=root)

    if is_array:
        if not isinstance(value, list):
            _raise_type(path, "array", error_cls)
        items = schema.get("items")
        if items is None:
            return list(value)
        return [
            _resolve_value(items, item, _index_path(path, index), error_cls)
            for index, item in enumerate(value)
        ]

    if kind is not None and not _matches_type(kind, value):
        _raise_type(path, kind, error_cls)

    _check_range(schema, value, path, error_cls)
    return value


# ---------------------------------------------------------------------------
# Public resolution API
# ---------------------------------------------------------------------------

def _root_default_base(
    schema: Mapping, supplied: Mapping[str, Any],
) -> Mapping[str, Any]:
    """Layer the root ``default`` object under property defaults and file values.

    Precedence is ``root default < property defaults < file values``: a key that
    also has a property-level ``default`` is dropped here so the property default
    wins; file values are merged last.
    """
    root_default = schema.get("default")
    if not isinstance(root_default, Mapping) or not root_default:
        return supplied
    properties = schema.get("properties") or {}
    base: dict = {}
    for key, value in root_default.items():
        subschema = properties.get(key)
        if isinstance(subschema, Mapping) and "default" in subschema:
            continue
        base[key] = value
    if supplied:
        base.update(supplied)
    return base


def resolve_plugin_config(
    manifest: PluginManifest,
    values: Optional[Mapping[str, Any]] = None,
) -> tuple[dict, dict[str, str]]:
    """Merge schema defaults with host-supplied values (file wins).

    The root ``default`` object is the lowest-precedence layer, property
    defaults come next, and file values win last.  Returns ``(resolved,
    sources)`` where ``sources`` maps each *top-level* key to ``"default"`` or
    ``"file"``.  A plugin without a ``config_schema`` may only receive an empty
    mapping, because every key would be unknown.
    """
    if values is None:
        supplied: Mapping[str, Any] = {}
    elif isinstance(values, Mapping):
        supplied = values
    else:
        raise PluginConfigError(
            "plugin config must be a mapping", key="config",
            hint="use a mapping of config keys to JSON values",
        )
    schema = manifest.config_schema
    if schema is None:
        if supplied:
            key = next(iter(supplied))
            raise PluginConfigError(
                f"unknown config key {key!r}",
                key=str(key),
                hint="the plugin declares no config_schema",
            )
        return {}, {}

    # Re-validate: manifests are also constructed in tests/hosts directly, and
    # an invalid schema must surface as E_PLUGIN_MANIFEST, not a value error.
    schema = validate_config_schema(schema)
    file_values = supplied
    merged = _root_default_base(schema, supplied)
    resolved = _resolve_value(
        schema, merged, "", PluginConfigError, root=True,
    )
    sources = {
        key: ("file" if key in file_values else "default")
        for key in resolved
    }
    return resolved, sources


def _secret_error(message: str, *, key: str, hint: str = "") -> PluginConfigError:
    """One secret-resolution failure, explicitly marked for host warnings (§4.6)."""
    return PluginConfigError(
        message, key=key, hint=hint, context={"secret": True},
    )


def resolve_plugin_secrets(
    manifest: PluginManifest,
    values: Optional[Mapping[str, str]],
    environ: Mapping[str, str],
) -> dict[str, str]:
    """Resolve every declared secret through an ``env:VAR`` host mapping.

    Every name in ``manifest.needs_secrets`` must have a mapping; undeclared
    names are rejected; the only accepted source is ``env:VAR``.  Errors carry
    the secret name and, when relevant, the variable name — never a value.
    Each error is marked with ``context={"secret": True}`` so the host can emit
    the ``plugin.secret_missing`` warning without string-sniffing keys.
    """
    if values is None:
        mappings: Mapping[str, str] = {}
    elif isinstance(values, Mapping):
        mappings = values
    else:
        raise _secret_error(
            "secret mappings must be a mapping", key="secrets",
            hint="use name -> 'env:VAR' pairs",
        )
    declared = tuple(manifest.needs_secrets)
    declared_set = set(declared)

    for name in mappings:
        if name not in declared_set:
            raise _secret_error(
                f"secret {name!r} is not declared by the plugin",
                key=str(name),
                hint="only names in needs_secrets may be mapped",
            )

    resolved: dict[str, str] = {}
    for name in declared:
        if name not in mappings:
            raise _secret_error(
                f"secret {name!r} has no host mapping",
                key=name,
                hint=f"set agent.plugins.secrets.<id>.{name}",
            )
        source = mappings[name]
        if (
            not isinstance(source, str)
            or not source.startswith("env:")
            or not source[len("env:"):]
        ):
            raise _secret_error(
                f"secret {name!r} must use an 'env:VAR' source",
                key=name,
                hint="secrets are only read from the environment via env:VAR",
            )
        variable = source[len("env:"):]
        if variable not in environ:
            raise _secret_error(
                f"environment variable for secret {name!r} is missing",
                key=name,
                hint=f"set environment variable {variable}",
            )
        resolved[name] = environ[variable]
    return resolved


def freeze_config(value: Any) -> Any:
    """Deep read-only snapshot: mappings become proxies, lists become tuples."""
    if isinstance(value, Mapping):
        return MappingProxyType({
            key: freeze_config(item) for key, item in value.items()
        })
    if isinstance(value, (list, tuple)):
        return tuple(freeze_config(item) for item in value)
    return value


__all__ = [
    "freeze_config",
    "resolve_plugin_config",
    "resolve_plugin_secrets",
    "validate_config_schema",
]
