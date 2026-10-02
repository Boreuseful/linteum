from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Literal,Any
import re
from .core import SkillMetadata,SkillSource

from ..tools import Session, Tool, ToolResult

class SkillRegistry:
    """
    Skill 发现、注册和延迟加载器。

    使用方式：

        registry = SkillRegistry(
            system_skill_dirs=[
                Path.home() / ".pi" / "agent" / "skills",
            ],
            project_skill_dirs=[
                Path.cwd() / ".pi" / "skills",
            ],
        )

        registry.discover()

        print(registry.build_prompt_index())

        skill = registry.load_skill("python-code-review")
        reference = registry.load_reference(
            "python-code-review",
            "references/security-checklist.md",
        )
    """

    SKILL_FILE_NAME = "SKILL.md"

    def __init__(
        self,
        *,
        system_skill_dirs: Iterable[str | Path] = (),
        project_skill_dirs: Iterable[str | Path] = (),
    ):
        self.sources: list[SkillSource] = [
            SkillSource(
                scope="system",
                root=Path(path).expanduser(),
                priority=100,
            )
            for path in system_skill_dirs
        ]

        self.sources.extend(
            SkillSource(
                scope="project",
                root=Path(path).expanduser(),
                priority=200,
            )
            for path in project_skill_dirs
        )

        # 保存所有同名候选 Skill，便于诊断和选择。
        self._candidates: dict[str, list[SkillMetadata]] = {}

        # 选中的 Skill，也就是实际使用的 Skill。
        self._selected: dict[str, SkillMetadata] = {}

        # 延迟加载后的内容缓存。
        self._content_cache: dict[Path, str] = {}

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    def discover(self) -> list[SkillMetadata]:
        """
        扫描所有来源目录，发现 SKILL.md 并注册元数据。

        返回所有发现的 Skill，包括被覆盖的候选项。
        """
        self._candidates.clear()
        self._selected.clear()
        self._content_cache.clear()

        discovered: list[SkillMetadata] = []

        for source in self.sources:
            root = source.root.expanduser().resolve()

            if not root.exists():
                continue

            if not root.is_dir():
                raise NotADirectoryError(
                    f"Skill source is not a directory: {root}"
                )

            for skill_file in self._find_skill_files(root):
                metadata = self._read_metadata(
                    skill_file=skill_file,
                    source=SkillSource(
                        scope=source.scope,
                        root=root,
                        priority=source.priority,
                    ),
                )

                self._candidates.setdefault(
                    metadata.name,
                    [],
                ).append(metadata)

                discovered.append(metadata)

        self._select_skills()
        return discovered

    def _find_skill_files(self, root: Path) -> list[Path]:
        """
        找到来源目录下所有 SKILL.md。

        每个包含 SKILL.md 的目录都被视为一个 Skill。
        """
        return sorted(
            path
            for path in root.rglob(self.SKILL_FILE_NAME)
            if path.is_file()
        )

    def _read_metadata(
        self,
        *,
        skill_file: Path,
        source: SkillSource,
    ) -> SkillMetadata:
        """
        读取 Skill 元数据。

        注意：这里虽然读取了文件内容，但只解析 frontmatter；
        正文不会进入 registry 的运行时提示词。
        """
        skill_file = skill_file.resolve()

        if not self._is_within(skill_file, source.root):
            raise ValueError(
                f"Skill file escapes source root: {skill_file}"
            )

        text = skill_file.read_text(encoding="utf-8")
        frontmatter = parse_frontmatter(text)

        name = frontmatter.get("name") or skill_file.parent.name
        description = frontmatter.get("description", "").strip()

        self._validate_skill_name(name)

        if not description:
            raise ValueError(
                f"Skill description is required: {skill_file}"
            )

        return SkillMetadata(
            name=name,
            description=description,
            path=skill_file,
            source=source,
        )

    def _select_skills(self) -> None:
        """
        为每个 Skill 名称选择一个实际生效的候选项。

        规则：

        1. priority 越高越优先；
        2. 同一来源下，路径排序靠后的 Skill 优先；
        3. 项目 Skill 默认覆盖系统 Skill。
        """
        for name, candidates in self._candidates.items():
            selected = max(
                candidates,
                key=lambda item: (
                    item.source.priority,
                    str(item.path),
                ),
            )
            self._selected[name] = selected

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    def names(self) -> list[str]:
        """
        返回当前生效的 Skill 名称。
        """
        return sorted(self._selected)

    def list_skills(self) -> list[SkillMetadata]:
        """
        返回当前生效的 Skill 元数据。
        """
        return [
            self._selected[name]
            for name in self.names()
        ]

    def get(self, name: str) -> SkillMetadata | None:
        """
        获取当前生效的 Skill 元数据。
        """
        return self._selected.get(name)

    def require(self, name: str) -> SkillMetadata:
        """
        获取 Skill；不存在时抛出明确异常。
        """
        skill = self.get(name)

        if skill is None:
            available = ", ".join(self.names()) or "(none)"
            raise KeyError(
                f"Unknown skill: {name}. "
                f"Available skills: {available}"
            )

        return skill

    def candidates(self, name: str) -> list[SkillMetadata]:
        """
        返回同名 Skill 的所有候选项。

        用于调试覆盖关系。
        """
        return list(self._candidates.get(name, []))

    def bind_session(self, session: Session) -> Session:
        """
        把当前生效的 Skill 记录到会话，返回该会话。

        等价于 ``session.register_skills(self.list_skills())``，会话随后可用
        ``session.registered_skills()`` 查看本会话注册了哪些 Skill。
        """
        session.register_skills(self.list_skills())
        return session

    # ------------------------------------------------------------------
    # Prompt index
    # ------------------------------------------------------------------

    def build_prompt_index(
        self,
        *,
        include_paths: bool = False,
        include_scope: bool = True,
    ) -> str:
        """
        构造可以注入 system prompt 的轻量 Skill 索引。

        不包含 SKILL.md 正文。
        """
        skills = self.list_skills()

        if not skills:
            return ""

        lines = [
            "## Available skills",
            "",
            (
                "Use the skill loader when a skill is relevant "
                "to the current task."
            ),
            "",
        ]

        for skill in skills:
            suffix: list[str] = []

            if include_scope:
                suffix.append(skill.source.scope)

            if suffix:
                label = f" [{', '.join(suffix)}]"
            else:
                label = ""

            lines.append(
                f"- `{skill.name}`{label}: "
                f"{skill.description}"
            )

            if include_paths:
                lines.append(f"  Path: `{skill.path}`")

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Lazy loading
    # ------------------------------------------------------------------

    def load_skill(
        self,
        name: str,
        *,
        use_cache: bool = True,
    ) -> str:
        """
        延迟加载完整的 SKILL.md。
        """
        skill = self.require(name)
        return self._read_text(
            skill.path,
            allowed_root=skill.root,
            use_cache=use_cache,
        )

    def create_load_skill(self)-> Tool:
        def execute(args:dict[str,Any], context: Session):
            try:
                value = self.load_skill(args.get("name", "."))
                return ToolResult(value)
            except Exception as exc:
                return ToolResult(str(exc), is_error=True)
        return Tool("load_skill", "延迟加载完整的 SKILL.md", {
            "type": "object", "properties": {"name": {"type": "string"}},
            "required": ["name"], "additionalProperties": False}, execute)

    def load_reference(
        self,
        skill_name: str,
        relative_path: str | Path,
        *,
        use_cache: bool = True,
    ) -> str:
        """
        延迟加载 Skill 目录内的附加资料。

        例如：

            registry.load_reference(
                "python-code-review",
                "references/security-checklist.md",
            )

        relative_path 必须是相对于 Skill 根目录的路径。
        """
        skill = self.require(skill_name)

        relative_path = Path(relative_path)

        if relative_path.is_absolute():
            raise ValueError(
                "Reference path must be relative: "
                f"{relative_path}"
            )

        reference_path = (skill.root / relative_path).resolve()

        if not self._is_within(reference_path, skill.root):
            raise ValueError(
                f"Reference path escapes skill directory: "
                f"{relative_path}"
            )

        if not reference_path.is_file():
            raise FileNotFoundError(reference_path)

        return self._read_text(
            reference_path,
            allowed_root=skill.root,
            use_cache=use_cache,
        )
    def create_load_reference(self)-> Tool:
        def execute(args:dict[str,Any], context: Session):
            try:
                value = self.load_reference(args["skill_name"], args["relative_path"])
                return ToolResult(value)
            except Exception as exc:
                return ToolResult(str(exc), is_error=True)
        return Tool("load_reference", "延迟加载 Skill 目录内的附加资料", {
            "type": "object", "properties": {"skill_name": {"type": "string"}, "relative_path": {"type": "string"}},
            "required": ["skill_name", "relative_path"], "additionalProperties": False}, execute)


    def _read_text(
        self,
        path: Path,
        *,
        allowed_root: Path,
        use_cache: bool,
    ) -> str:
        """
        受限文件读取。

        即使调用方传入了符号链接，也要在 resolve 后重新检查路径。
        """
        path = path.resolve()
        allowed_root = allowed_root.resolve()

        if not self._is_within(path, allowed_root):
            raise ValueError(
                f"File is outside allowed root: {path}"
            )

        if not path.is_file():
            raise FileNotFoundError(path)

        if use_cache and path in self._content_cache:
            return self._content_cache[path]

        content = path.read_text(encoding="utf-8")

        if use_cache:
            self._content_cache[path] = content

        return content

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------

    def conflicts(self) -> dict[str, list[SkillMetadata]]:
        """
        返回存在多个候选项的同名 Skill。
        """
        return {
            name: candidates
            for name, candidates in self._candidates.items()
            if len(candidates) > 1
        }

    def describe(self, name: str) -> str:
        """
        返回一个 Skill 的来源和覆盖信息。
        """
        selected = self.require(name)
        candidates = self.candidates(name)

        lines = [
            f"name: {selected.name}",
            f"description: {selected.description}",
            f"selected_path: {selected.path}",
            f"selected_scope: {selected.source.scope}",
            "",
            "candidates:",
        ]

        for candidate in candidates:
            marker = "*" if candidate.path == selected.path else " "
            lines.append(
                f"{marker} "
                f"[{candidate.source.scope}] "
                f"{candidate.path}"
            )

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Validation helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _is_within(path: Path, root: Path) -> bool:
        """
        判断 path 是否位于 root 内部或就是 root 本身。
        """
        try:
            path.relative_to(root)
            return True
        except ValueError:
            return False

    @staticmethod
    def _validate_skill_name(name: str) -> None:
        """
        Skill 名称只允许安全的标识符形式。
        """
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name):
            raise ValueError(
                "Invalid skill name. Expected only letters, "
                f"numbers, '.', '_' or '-': {name!r}"
            )


def parse_frontmatter(text: str) -> dict[str, str]:
    """
    解析简单 YAML 风格 frontmatter。

    支持：

        ---
        name: python-code-review
        description: Review Python code
        ---

    该解析器适用于简单字符串字段。
    如果需要支持数组、嵌套对象、多行字符串等 YAML 特性，
    建议改用 yaml.safe_load。
    """
    match = re.match(
        r"\A\s*---[ \t]*\n"
        r"(.*?)"
        r"\n---[ \t]*(?:\n|$)",
        text,
        flags=re.DOTALL,
    )

    if not match:
        return {}

    result: dict[str, str] = {}

    for raw_line in match.group(1).splitlines():
        line = raw_line.strip()

        if not line:
            continue

        if line.startswith("#"):
            continue

        if ":" not in line:
            continue

        key, value = line.split(":", 1)

        key = key.strip()
        value = value.strip()

        if (
            len(value) >= 2
            and value[0] == value[-1]
            and value[0] in {"'", '"'}
        ):
            value = value[1:-1]

        result[key] = value

    return result


if __name__ == "__main__":
    project_root = Path.cwd()

    registry = SkillRegistry(
        system_skill_dirs=[
            Path.home() / ".pi" / "agent" / "skills",
        ],
        project_skill_dirs=[
            project_root / ".pi" / "skills",
        ],
    )

    discovered = registry.discover()

    print(f"Discovered skills: {len(discovered)}")
    print()

    print(registry.build_prompt_index(include_paths=True))
    print()

    for name in registry.names():
        print(registry.describe(name))
        print()

    conflicts = registry.conflicts()

    if conflicts:
        print("Skill conflicts:")
        for name, candidates in conflicts.items():
            print(f"- {name}:")
            for candidate in candidates:
                print(f"  - [{candidate.source.scope}] {candidate.path}")