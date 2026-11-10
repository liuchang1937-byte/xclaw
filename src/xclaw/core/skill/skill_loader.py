"""发现目录型 SKILL.md 和旧单文件 Skill；正文在调用时读取。"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml
from loguru import logger

from xclaw.core.execution import Capability
from xclaw.core.tool import Tool, ToolRegistry

NAME_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def parse_frontmatter(content: str) -> tuple[dict[str, Any], str]:
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n?", content.lstrip("\ufeff"), re.DOTALL)
    if not match:
        return {}, content
    data = yaml.safe_load(match.group(1)) or {}
    if not isinstance(data, dict):
        raise ValueError("Skill frontmatter 必须是对象")
    return data, content.lstrip("\ufeff")[match.end() :]


class Skill:
    def __init__(
        self,
        name: str,
        description: str,
        prompt_template: str = "",
        tools_needed: list[str] | None = None,
        triggers: list[str] | None = None,
        source_file: str = "",
        metadata: dict[str, str] | None = None,
    ):
        self.name = name
        self.description = description
        self._prompt_template = prompt_template
        self.tools_needed = tools_needed or []
        self.triggers = triggers or []
        self.source_file = source_file
        self._root = Path(source_file).resolve().parent if source_file else None
        self.metadata = metadata or {}
        self.registry: ToolRegistry | None = None

    @property
    def root(self) -> Path:
        if self._root is None:
            raise ValueError("内联 Skill 没有资源目录")
        return self._root

    @property
    def prompt_template(self) -> str:
        if not self.source_file:
            return self._prompt_template
        source = Path(self.source_file)
        try:
            source.resolve().relative_to(self.root)
        except ValueError as exc:
            raise ValueError("Skill 入口路径超出原始资源目录") from exc
        if source.stat().st_size > 128 * 1024:
            raise ValueError("SKILL.md 超过 128 KiB，请拆分参考资料")
        metadata, body = parse_frontmatter(source.read_text(encoding="utf-8"))
        if metadata.get("name") != self.name:
            raise ValueError("Skill 名称已变化，请重新加载技能目录")
        if not body.strip():
            raise ValueError("Skill 正文不能为空")
        return body.strip()

    def resource_path(self, relative: str) -> Path:
        path = Path(relative)
        if path.is_absolute() or ".." in path.parts or "\\" in relative:
            raise ValueError("资源路径必须是 Skill 内的相对路径，使用 '/' 分隔")
        resolved = (self.root / path).resolve()
        try:
            resolved.relative_to(self.root)
        except ValueError as exc:
            raise ValueError("资源路径超出 Skill 目录") from exc
        if not resolved.is_file():
            raise ValueError(f"Skill 资源不存在: {relative}")
        return resolved

    def entrypoint(self) -> Path:
        relative = self.metadata.get("entrypoint", "")
        if not relative.startswith("scripts/") or not relative.endswith(".py"):
            raise ValueError("此 Skill 未声明 Python 脚本入口 metadata.entrypoint")
        return self.resource_path(relative)

    async def execute(self, user_input: str = "", **kwargs) -> str:
        parts = [f"## Skill: {self.name}"]
        if self.metadata.get("requirements"):
            parts.append(f"依赖说明: {self.metadata['requirements']}")
        if self.source_file:
            parts.append(
                f"资源通过 read_skill_resource(skill_name='{self.name}', path='...') 按需读取。"
                f"脚本通过 run_skill_script(skill_name='{self.name}', arguments=[...]) 执行。"
            )
        if self.registry:
            unavailable = [
                name for name in self.tools_needed if self.registry.get_definition(name) is None
            ]
            if unavailable:
                parts.append(
                    f"当前不可用的依赖工具: {', '.join(unavailable)}；Skill 不会提升执行权限。"
                )
        if user_input:
            parts.append(f"## 用户需求\n{user_input}")
        parts.append(self.prompt_template)
        return "\n\n".join(parts)

    def to_tool(self) -> Tool:
        return Tool(
            name=self.name,
            description=f"【Skill】{self.description}",
            func=self.execute,
            parallel_safe=True,
            capabilities=frozenset({Capability.PROMPT}),
            parameters={
                "type": "object",
                "properties": {"user_input": {"type": "string"}},
                "required": ["user_input"],
                "additionalProperties": False,
            },
        )


class SkillLoader:
    def __init__(self, skills_dir: str = "skills"):
        self.skills_dir = Path(skills_dir).resolve()

    def load_all(self) -> list[Skill]:
        if not self.skills_dir.is_dir():
            logger.warning(f"Skills 目录不存在: {self.skills_dir}")
            return []
        # 目录型优先；兼容旧的 skills/name.md，不递归加载参考文档。
        sources = sorted(self.skills_dir.glob("*/SKILL.md")) + sorted(self.skills_dir.glob("*.md"))
        found = {}
        for source in sources:
            skill = self._load_one(source)
            if not skill:
                continue
            if skill.name in found:
                logger.warning(f"跳过重复 Skill: {skill.name} ({source})")
                continue
            found[skill.name] = skill
        return list(found.values())

    def _load_one(self, source: Path) -> Skill | None:
        try:
            source.resolve().relative_to(self.skills_dir)
            # 只读取 frontmatter，避免启动时读取所有正文和资源。
            lines = []
            size = 0
            with source.open(encoding="utf-8-sig") as stream:
                first = stream.readline(16385)
                if first.strip() != "---":
                    return None
                lines.append(first)
                for line in stream:
                    size += len(line)
                    if size > 16384:
                        raise ValueError("Skill 元数据超过 16 KiB")
                    lines.append(line)
                    if line.strip() == "---":
                        break
                else:
                    raise ValueError("Skill 元数据缺少结束标记")
            frontmatter, _ = parse_frontmatter("".join(lines))
            name, description = frontmatter.get("name"), frontmatter.get("description")
            if not isinstance(name, str) or len(name) > 64 or not NAME_PATTERN.fullmatch(name):
                raise ValueError("Skill 名称必须是 1～64 字符的小写字母/数字/连字符名称")
            if source.name == "SKILL.md" and name != source.parent.name:
                raise ValueError("Skill 名称必须与目录名一致")
            if (
                not isinstance(description, str)
                or not description.strip()
                or len(description) > 1024
            ):
                raise ValueError("Skill description 必须是 1～1024 字符的文本")
            metadata = frontmatter.get("metadata") or {}
            if not isinstance(metadata, dict) or any(
                not isinstance(key, str) or not isinstance(value, str)
                for key, value in metadata.items()
            ):
                raise ValueError("Skill metadata 必须是字符串键值对")
            needed = frontmatter.get("tools_needed", metadata.get("tools", "").split())
            triggers = frontmatter.get("triggers", [])
            for value in (needed, triggers):
                if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
                    raise ValueError("Skill tools_needed/triggers 必须是字符串数组")
            return Skill(
                name,
                " ".join(description.split()),
                source_file=str(source.resolve()),
                metadata=metadata,
                tools_needed=needed,
                triggers=triggers,
            )
        except (OSError, ValueError, yaml.YAMLError) as exc:
            logger.warning(f"跳过无效 Skill {source}: {exc}")
            return None

    _parse_frontmatter = staticmethod(parse_frontmatter)
