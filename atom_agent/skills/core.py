from dataclasses import dataclass
from pathlib import Path
from typing import Literal


SkillScope = Literal["system", "project"]



@dataclass(frozen=True)
class SkillSource:
    """
    Skill 的来源。

    priority 越大，优先级越高。
    """

    scope: SkillScope
    root: Path
    priority: int


@dataclass(frozen=True)
class SkillMetadata:
    """
    Skill 注册阶段保存的轻量元数据。

    注册阶段只需要这些信息，不加载完整 SKILL.md 正文。
    """

    name: str
    description: str
    path: Path
    source: SkillSource

    @property
    def root(self) -> Path:
        """
        Skill 目录，例如：

        /project/.pi/skills/code-review
        """
        return self.path.parent