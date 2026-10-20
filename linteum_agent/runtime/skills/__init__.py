"""Skills layer: discover, catalog and progressively disclose skill packages.

Public surface used by the host:

* :mod:`linteum_agent.runtime.skills.model` — immutable catalog value objects;
* :mod:`linteum_agent.runtime.skills.source` — ``SkillSource`` / directory source;
* :mod:`linteum_agent.runtime.skills.parsing` — ``SKILL.md`` + resource loading;
* :mod:`linteum_agent.runtime.skills.catalog` — deterministic build/index/digest;
* :mod:`linteum_agent.runtime.skills.tools` — ``load_skill`` / ``read_skill_resource``.

The layer is a **consumer** of plugin-provided content: a plugin with only
tools never reaches this package, and a skill is never registered as a tool.
"""

from __future__ import annotations

from .errors import (
    SkillLimitExceeded,
    SkillNotFound,
    SkillPathViolation,
    SkillResourceNotFound,
)
from .model import (
    RESOURCE_DIRS,
    RESOURCE_KINDS,
    SKILL_NAME_PATTERN,
    SkillCatalog,
    SkillEntry,
    SkillLimits,
    SkillPackage,
    SkillResource,
)

__all__ = [
    "RESOURCE_DIRS",
    "RESOURCE_KINDS",
    "SKILL_NAME_PATTERN",
    "SkillCatalog",
    "SkillEntry",
    "SkillLimits",
    "SkillLimitExceeded",
    "SkillNotFound",
    "SkillPackage",
    "SkillPathViolation",
    "SkillResource",
    "SkillResourceNotFound",
]
