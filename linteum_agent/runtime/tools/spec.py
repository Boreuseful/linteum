"""ToolSpec and the dependency-free JSON-Schema validator (§21).

A :class:`ToolSpec` is the *only* thing the runtime and the LLM ever see about a
tool: a frozen, JSON-serialisable description plus the live callable kept
**in-process** (P6 — the function object never crosses the bus; only its ``name``
travels as an event payload, §21.1).

The design document validates arguments with :mod:`jsonschema`.  To keep the
package pure-standard-library (matching the rest of the project) this module
implements the small subset of JSON Schema that tools actually use — object
properties, primitive types, ``required``, ``additionalProperties``, ``enum``,
numeric / string / array bounds — and raises :class:`~linteum_agent.runtime.tools.errors.ValidationError`.
"""

from __future__ import annotations

import inspect
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .errors import ValidationError

#: Allowed values of :attr:`ToolSpec.side_effect` (§21).
SIDE_EFFECTS: tuple = ("pure", "read", "write", "dangerous")
#: Allowed values of :attr:`ToolSpec.backend` (§23).
BACKENDS: tuple = ("inproc", "thread", "subproc", "wasm", "remote")

#: Which Python types satisfy a JSON-Schema ``type`` keyword.
_TYPE_CHECKS: dict = {
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "array": lambda v: isinstance(v, (list, tuple)),
    "object": lambda v: isinstance(v, dict),
    "null": lambda v: v is None,
}


def _matches(value: Any, type_name: str) -> bool:
    check = _TYPE_CHECKS.get(type_name)
    if check is None:  # unknown type keyword: do not fail the whole call
        return True
    return check(value)


def _validate_value(value: Any, schema: dict, *, path: str) -> None:
    """Validate one ``value`` against one property ``schema`` (recursive)."""
    if not isinstance(schema, dict):
        return

    if "type" in schema:
        declared = schema["type"]
        types = declared if isinstance(declared, (list, tuple)) else [declared]
        if not any(_matches(value, t) for t in types):
            raise ValidationError(f"{path!r}: expected {declared}, got {type(value).__name__}")

    if "enum" in schema and value not in schema["enum"]:
        raise ValidationError(f"{path!r}: {value!r} is not one of {schema['enum']!r}")

    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            raise ValidationError(f"{path!r}: shorter than minLength {schema['minLength']}")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            raise ValidationError(f"{path!r}: longer than maxLength {schema['maxLength']}")
        if "pattern" in schema and not re.search(schema["pattern"], value):
            raise ValidationError(f"{path!r}: does not match pattern {schema['pattern']!r}")
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            raise ValidationError(f"{path!r}: below minimum {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            raise ValidationError(f"{path!r}: above maximum {schema['maximum']}")
    elif isinstance(value, (list, tuple)):
        if "minItems" in schema and len(value) < schema["minItems"]:
            raise ValidationError(f"{path!r}: fewer than minItems {schema['minItems']}")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            raise ValidationError(f"{path!r}: more than maxItems {schema['maxItems']}")
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(value):
                _validate_value(item, item_schema, path=f"{path}[{index}]")
    elif isinstance(value, dict):
        _validate_object(value, schema, path=path)


def _validate_object(value: dict, schema: dict, *, path: str) -> None:
    properties = schema.get("properties") or {}
    for key in schema.get("required", ()) or ():
        if key not in value:
            raise ValidationError(f"{path}.{key}: required property missing")
    additional = schema.get("additionalProperties", True)
    for key, item in value.items():
        child = f"{path}.{key}" if path != "" else key
        if key in properties:
            _validate_value(item, properties[key], path=child)
        elif additional is False:
            raise ValidationError(f"{child!r}: additional properties are not allowed")


def validate_args(
    params: dict,
    args: Any,
    *,
    required: tuple = (),
    additional_properties: bool = False,
) -> dict:
    """Validate ``args`` against the tool's ``params`` and return it unchanged."""
    if not isinstance(args, dict):
        raise ValidationError(f"args must be an object, got {type(args).__name__}")
    schema = {
        "type": "object",
        "properties": params,
        "required": list(required),
        "additionalProperties": additional_properties,
    }
    _validate_object(args, schema, path="")
    return args


@dataclass(frozen=True)
class ToolSpec:
    """Frozen description of one tool (§21).

    Only ``name`` (a string) ever reaches the bus; ``fn`` stays in-process and is
    reachable exclusively through the assembly-time
    :class:`~linteum_agent.runtime.tools.registry.ToolRegistry`.
    """

    name: str
    fn: Callable
    description: str = ""
    params: dict = field(default_factory=dict)
    required_params: tuple = ()
    side_effect: str = "read"
    timeout: float = 30.0
    idempotent: bool = True
    concurrency: int = 4
    requires_approval: bool = False
    backend: str = "inproc"
    plugin_id: Optional[str] = None
    #: Optional dotted ``module:qualname`` used by out-of-process backends (§23).
    entrypoint: Optional[str] = None

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name:
            raise ValueError("ToolSpec.name must be a non-empty string")
        if self.side_effect not in SIDE_EFFECTS:
            raise ValueError(f"side_effect must be one of {SIDE_EFFECTS}, got {self.side_effect!r}")
        if self.backend not in BACKENDS:
            raise ValueError(f"backend must be one of {BACKENDS}, got {self.backend!r}")
        if self.timeout <= 0:
            raise ValueError("timeout must be positive")
        if self.concurrency <= 0:
            raise ValueError("concurrency must be positive")
        # normalise ``required_params`` to a tuple (accepts a list from the decorator)
        object.__setattr__(self, "required_params", tuple(self.required_params or ()))

    # -- validation / schema -------------------------------------------------
    def validate(self, args: Any) -> dict:
        """Schema-check ``args`` and return them (raises ``ValidationError``)."""
        return validate_args(self.params, args or {}, required=self.required_params)

    def json_schema(self) -> dict:
        """The function-calling schema handed to the provider (§21)."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": dict(self.params),
                    "required": list(self.required_params),
                },
            },
        }

    # -- sandbox hints -------------------------------------------------------
    @property
    def qualified_name(self) -> str:
        """Dotted ``module:qualname`` for out-of-process backends."""
        if self.entrypoint:
            return self.entrypoint
        return f"{self.fn.__module__}:{self.fn.__qualname__}"

    def accepts_progress(self) -> bool:
        """True when ``fn`` declares a ``progress`` parameter (streaming tools)."""
        try:
            signature = inspect.signature(self.fn)
        except (TypeError, ValueError):
            return False
        return "progress" in signature.parameters

    def is_async(self) -> bool:
        return inspect.iscoroutinefunction(self.fn)

