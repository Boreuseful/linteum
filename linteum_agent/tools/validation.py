"""Small dependency-free JSON Schema validator for tool arguments."""

from __future__ import annotations

from typing import Any


def validate_arguments(schema: dict[str, Any], value: Any) -> dict[str, Any]:
    errors: list[str] = []
    _validate(schema or {}, value, "$", errors)
    if errors:
        raise ValueError("tool argument validation failed: " + "; ".join(errors))
    return value


def _validate(schema: dict[str, Any], value: Any, path: str, errors: list[str]) -> None:
    if "const" in schema and value != schema["const"]:
        errors.append(f"{path} must equal {schema['const']!r}")
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path} must be one of the enum values")
    kind = schema.get("type")
    if kind and not _is_type(value, kind):
        errors.append(f"{path} must be of type {kind}")
        return
    if isinstance(value, dict):
        required = schema.get("required", [])
        for key in required:
            if key not in value:
                errors.append(f"{path}.{key} is a required argument")
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            for key in value:
                if key not in properties:
                    errors.append(f"{path}.{key} is not a declared argument")
        for key, item_schema in properties.items():
            if key in value and isinstance(item_schema, dict):
                _validate(item_schema, value[key], f"{path}.{key}", errors)
        _limits(schema, value, path, errors)
    elif isinstance(value, list):
        if isinstance(schema.get("items"), dict):
            for index, item in enumerate(value):
                _validate(schema["items"], item, f"{path}[{index}]", errors)
        _limits(schema, value, path, errors)
    else:
        _limits(schema, value, path, errors)


def _is_type(value: Any, kind: str | list[str]) -> bool:
    kinds = [kind] if isinstance(kind, str) else kind
    return any({
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        "boolean": isinstance(value, bool),
        "null": value is None,
    }.get(item, True) for item in kinds)


def _limits(schema: dict[str, Any], value: Any, path: str, errors: list[str]) -> None:
    size = len(value) if isinstance(value, (str, list, dict)) else value
    if "minLength" in schema and isinstance(value, str) and len(value) < schema["minLength"]:
        errors.append(f"{path} length must not be less than {schema['minLength']}")
    if "maxLength" in schema and isinstance(value, str) and len(value) > schema["maxLength"]:
        errors.append(f"{path} length must not be greater than {schema['maxLength']}")
    if "minItems" in schema and isinstance(value, list) and len(value) < schema["minItems"]:
        errors.append(f"{path} item count must not be less than {schema['minItems']}")
    if "maxItems" in schema and isinstance(value, list) and len(value) > schema["maxItems"]:
        errors.append(f"{path} item count must not be greater than {schema['maxItems']}")
    if "minimum" in schema and isinstance(value, (int, float)) and value < schema["minimum"]:
        errors.append(f"{path} must not be less than {schema['minimum']}")
    if "maximum" in schema and isinstance(value, (int, float)) and value > schema["maximum"]:
        errors.append(f"{path} must not be greater than {schema['maximum']}")
