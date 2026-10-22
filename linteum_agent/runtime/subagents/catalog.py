"""Reserved tool schema + policy resolution for subagent spawns (§6.1, §7.3).

The model sees one ordinary function schema (``spawn_subagent``); the reducer
routes the call by *name* before ``ToolRuntime`` ever sees it.  This module owns
the pure part of the boundary: the schema text and the rule that a model may
only **narrow** the host's tool/model allowlist.  Violations raise
:class:`~linteum_agent.runtime.subagents.errors.SubagentError`; they never
silently degrade to a smaller capability set.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from ...config.schema import SubagentSettings
from ..state.agent_state import Budget
from .errors import (
    E_SUBAGENT_DEPTH,
    E_SUBAGENT_INVALID,
    E_SUBAGENT_POLICY,
    E_SUBAGENT_UNSUPPORTED,
    SubagentError,
)
from .model import SubagentPolicy

#: Tools whose side effects make the child a writer (never a read-only child).
_WRITE_EFFECTS = frozenset({"write", "dangerous"})


def normalise_title(value: Any, task: str) -> str:
    """Return a short display title, falling back to the task's first line.

    The model is asked for a title in the tool schema, but an older event or a
    provider that drops the field must still produce a usable label.
    """
    if isinstance(value, str):
        for line in value.splitlines():
            title = line.strip()
            if title:
                return title
    for line in str(task).splitlines():
        title = line.strip()
        if title:
            return title
    return "subagent"


class SubagentCatalog:
    """Schema + policy resolver bound to one host :class:`SubagentSettings`."""

    def __init__(self, settings: SubagentSettings) -> None:
        self.settings = settings

    @property
    def tool_name(self) -> str:
        return self.settings.tool_name

    # -- schema -------------------------------------------------------------
    def schema(self) -> dict:
        """The provider-facing function schema (design §6.1)."""
        return {
            "type": "function",
            "function": {
                "name": self.settings.tool_name,
                "description": (
                    "Delegate a task to an independent child agent with its own "
                    "context. The child cannot see this conversation; it only "
                    "sees the task/instructions/context/environment you write."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "title": {
                            "type": "string",
                            "description": (
                                "A short human-readable title (at most 60 "
                                "characters) naming the delegated goal, for UI "
                                "display. Use the user's language."
                            ),
                        },
                        "task": {
                            "type": "string",
                            "description": "The concrete task to complete.",
                        },
                        "instructions": {
                            "type": "string",
                            "description": (
                                "The child's role, constraints and prompt. It "
                                "cannot widen tools, models or budget."
                            ),
                        },
                        "context": {
                            "type": "string",
                            "description": (
                                "Background, relevant files and known facts."
                            ),
                        },
                        "environment": {
                            "type": "string",
                            "description": "Task-specific environment notes.",
                        },
                        "expected_output": {
                            "type": "string",
                            "description": (
                                "Delivery format and completion criteria."
                            ),
                        },
                        "tools": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "Optional tool subset; must be a subset of the "
                                "host allowlist."
                            ),
                        },
                        "model_profile": {
                            "type": "string",
                            "description": (
                                "Optional host profile alias; must be in the "
                                "host allowlist."
                            ),
                        },
                        "timeout_ms": {"type": "integer", "minimum": 1000},
                    },
                    "required": ["title", "task", "instructions"],
                    "additionalProperties": False,
                },
            },
        }

    # -- policy -------------------------------------------------------------
    def resolve(
        self,
        call_id: str,
        args: Mapping[str, Any],
        *,
        parent_tools: Sequence[str],
        tool_side_effects: Mapping[str, str],
        profiles: Sequence[str],
    ) -> SubagentPolicy:
        """Validate ``args`` and return the effective policy, or raise.

        ``parent_tools`` is the root session's live registry (minus the reserved
        name); ``tool_side_effects`` maps each available tool to its
        ``ToolSpec.side_effect``; ``profiles`` is ``agent.profile_names()``.
        """
        settings = self.settings
        if not isinstance(args, Mapping):
            raise self._invalid("spawn arguments must be an object", call_id)

        title = args.get("title")
        if title is not None and not isinstance(title, str):
            raise self._invalid("title must be a string", call_id)

        task = args.get("task")
        instructions = args.get("instructions")
        for name, value in (("task", task), ("instructions", instructions)):
            if not isinstance(value, str) or not value.strip():
                raise self._invalid(f"{name} must be a non-empty string", call_id)

        optional: dict[str, str] = {}
        for name in ("context", "environment", "expected_output"):
            value = args.get(name)
            if value is None:
                value = ""
            if not isinstance(value, str):
                raise self._invalid(f"{name} must be a string", call_id)
            optional[name] = value

        if args.get("depends_on"):
            raise SubagentError(
                E_SUBAGENT_UNSUPPORTED,
                "depends_on is reserved but dependency scheduling is not implemented",
                context={"call_id": call_id},
            )

        prompt_bytes = sum(
            len(value.encode("utf-8"))
            for value in (task, instructions, *optional.values())
        )
        if prompt_bytes > settings.max_prompt_bytes:
            raise self._invalid(
                f"prompt is {prompt_bytes}B > max_prompt_bytes "
                f"{settings.max_prompt_bytes}",
                call_id,
                prompt_bytes=prompt_bytes,
            )

        effective = self._resolve_tools(
            args.get("tools"), call_id,
            parent_tools=tuple(dict.fromkeys(parent_tools)),
        )
        model = self._resolve_model(args.get("model_profile"), call_id,
                                    tuple(profiles))
        timeout, clamped = self._resolve_timeout(args.get("timeout_ms"), call_id)

        side_effects = [tool_side_effects.get(name, "dangerous") for name in effective]
        read_only = not any(effect in _WRITE_EFFECTS for effect in side_effects)
        budget = Budget(
            tokens=settings.max_tokens,
            usd=settings.max_usd,
            rounds=settings.max_rounds,
            wall_ms=settings.max_wall_ms,
        )
        return SubagentPolicy(
            tools=effective,
            model_profile=model,
            read_only=read_only,
            timeout_ms=timeout,
            timeout_clamped=clamped,
            approval_policy=settings.approval_policy,
            budget=budget,
        )

    # -- pieces -------------------------------------------------------------
    def _resolve_tools(
        self, requested: Any, call_id: str, *, parent_tools: tuple[str, ...]
    ) -> tuple[str, ...]:
        settings = self.settings
        parent_set = set(parent_tools)
        allowed = tuple(dict.fromkeys(
            settings.allowed_tools or parent_tools
        ))
        allowed_set = set(allowed)

        if requested is None:
            if settings.default_tools:
                effective = tuple(dict.fromkeys(settings.default_tools))
            else:
                effective = tuple(
                    name for name in parent_tools if name != settings.tool_name
                )
        else:
            if not isinstance(requested, (list, tuple)):
                raise self._invalid("tools must be an array of names", call_id)
            if not requested:
                raise self._invalid("tools must contain at least one name", call_id)
            names: list[str] = []
            for item in requested:
                if not isinstance(item, str) or not item.strip():
                    raise self._invalid("tool names must be non-empty strings",
                                        call_id)
                names.append(item)
            effective = tuple(dict.fromkeys(names))

        if not effective:
            raise self._invalid("no tools are available for the child", call_id)
        for name in effective:
            if name == settings.tool_name:
                raise SubagentError(
                    E_SUBAGENT_DEPTH,
                    f"{name!r} would exceed max_depth={settings.max_depth}",
                    context={"call_id": call_id, "tool": name},
                )
            if name not in allowed_set:
                raise SubagentError(
                    E_SUBAGENT_POLICY,
                    f"tool {name!r} is not in the host allowlist",
                    context={
                        "call_id": call_id,
                        "requested_tools": list(effective),
                        "allowed_tools": list(allowed),
                    },
                )
            if name not in parent_set:
                raise SubagentError(
                    E_SUBAGENT_POLICY,
                    f"tool {name!r} is not available in this workspace",
                    context={
                        "call_id": call_id,
                        "requested_tools": list(effective),
                        "available_tools": list(parent_tools),
                    },
                )
        return effective

    def _resolve_model(self, requested: Any, call_id: str,
                       profiles: tuple[str, ...]) -> str:
        settings = self.settings
        if requested is None:
            model = settings.default_model_profile
        else:
            if not isinstance(requested, str) or not requested.strip():
                raise self._invalid("model_profile must be a non-empty string",
                                    call_id)
            model = requested
        allowed = set(settings.allowed_model_profiles
                      or (settings.default_model_profile,))
        if model not in allowed or model not in set(profiles):
            raise SubagentError(
                E_SUBAGENT_POLICY,
                f"model profile {model!r} is not in the host allowlist",
                context={
                    "call_id": call_id,
                    "model_profile": model,
                    "allowed_model_profiles": sorted(allowed),
                },
            )
        return model

    def _resolve_timeout(self, requested: Any, call_id: str) -> tuple[int, bool]:
        settings = self.settings
        if requested is None:
            timeout = settings.default_timeout_ms
        else:
            if isinstance(requested, bool) or not isinstance(requested, (int, float)):
                raise self._invalid("timeout_ms must be an integer", call_id)
            timeout = int(requested)
            if timeout <= 0:
                raise self._invalid("timeout_ms must be positive", call_id)
        if timeout > settings.max_timeout_ms:
            return settings.max_timeout_ms, True
        return timeout, False

    @staticmethod
    def _invalid(message: str, call_id: str, **context: Any) -> SubagentError:
        return SubagentError(
            E_SUBAGENT_INVALID, message,
            context={"call_id": call_id, **context},
        )


__all__ = ["SubagentCatalog", "normalise_title"]
