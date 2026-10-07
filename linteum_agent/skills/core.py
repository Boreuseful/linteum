from dataclasses import dataclass
from pathlib import Path
from typing import Literal


SkillScope = Literal["system", "project"]



@dataclass(frozen=True)
class SkillSource:
    """
    The source of a Skill.

    The larger the priority, the higher the precedence.
    """

    scope: SkillScope
    root: Path
    priority: int


@dataclass(frozen=True)
class SkillMetadata:
    """
    Lightweight metadata saved during the Skill registration stage.

    Only this information is needed at the registration stage; the full SKILL.md body is not loaded.
    """

    name: str
    description: str
    path: Path
    source: SkillSource

    @property
    def root(self) -> Path:
        """
        The Skill directory, for example:

        /project/.pi/skills/code-review
        """
        return self.path.parent