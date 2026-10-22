"""Skill-layer errors (tool errors with skill-specific codes).

The skills layer never raises onto the bus: the two loader tools are ordinary
:class:`~linteum_agent.runtime.tools.spec.ToolSpec` functions, so
:class:`~linteum_agent.runtime.tools.runtime.ToolRuntime` converts these into
``tool.call.failed`` events carrying ``error_info``.  The codes are distinct
from ``E_UNKNOWN_TOOL`` so the model can tell "this skill does not exist" from
"the tool name was hallucinated".
"""

from __future__ import annotations

from ...bus.errors import ErrorCode
from ..tools.errors import ToolError


class SkillNotFound(ToolError):
    """``load_skill`` was given a name that is not in the session catalog."""

    code = ErrorCode.E_SKILL_NOT_FOUND.value


class SkillResourceNotFound(ToolError):
    """``read_skill_resource`` was given a path that is not in the resource list."""

    code = ErrorCode.E_SKILL_RESOURCE_NOT_FOUND.value


class SkillPathViolation(ToolError):
    """The model tried to read outside the skill root (policy denial).

    ``safety=True`` matches :class:`~linteum_agent.runtime.tools.errors.WorkspaceViolation`'s
    policy kinds: the agent renders an explicit marker and should stop rather
    than retry with another path.
    """

    code = ErrorCode.E_SKILL_PATH.value
    safety = True


class SkillLimitExceeded(ToolError):
    """An offset or size is outside the configured skill limits (retryable)."""

    code = ErrorCode.E_SKILL_LIMIT.value
    retryable = True


__all__ = [
    "SkillLimitExceeded",
    "SkillNotFound",
    "SkillPathViolation",
    "SkillResourceNotFound",
]
