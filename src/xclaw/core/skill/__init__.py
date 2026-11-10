"""Skill 包：兼容原有导入，支持目录发现、按需资源读取和受控脚本执行。"""

from pathlib import Path

from loguru import logger

from xclaw.core.skill.executor import register_skill_helpers
from xclaw.core.skill.skill_loader import Skill, SkillLoader
from xclaw.core.tool import ToolRegistry

__all__ = ["Skill", "SkillLoader", "register_skills_as_tools"]


def register_skills_as_tools(registry: ToolRegistry, skills_dir: str = "skills") -> list[Skill]:
    loader = SkillLoader(skills_dir)
    registered = []
    for skill in loader.load_all():
        if skill.name in registry or skill.name in {"read_skill_resource", "run_skill_script"}:
            logger.warning(f"Skill 名称与已注册工具冲突，跳过: {skill.name}")
            continue
        skill.registry = registry
        registry.add_tool(skill.to_tool())
        registered.append(skill)
    if registered:
        if registry._sandbox_router and any(
            skill.metadata.get("entrypoint") for skill in registered
        ):
            registry._sandbox_router.docker.configure_skills(Path(skills_dir).resolve())
        register_skill_helpers(registry, registered, loader.skills_dir)
    return registered
