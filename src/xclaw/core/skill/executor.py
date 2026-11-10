"""资源读取与固定入口脚本执行；技能说明不能修改运行权限。"""

from __future__ import annotations

from pathlib import Path

from xclaw.core.concurrency import run_blocking
from xclaw.core.execution import Capability, RunProcess
from xclaw.core.skill.skill_loader import Skill
from xclaw.core.tool import Tool, ToolRegistry
from xclaw.sandbox.docker import DockerSandbox


def register_skill_helpers(registry: ToolRegistry, skills: list[Skill], root: Path) -> None:
    if any(name in registry for name in ("read_skill_resource", "run_skill_script")):
        raise ValueError("Skill 辅助工具的保留名称已被占用")
    catalog = {skill.name: skill for skill in skills}

    def lookup(name):
        if name not in catalog:
            raise ValueError(f"未知 Skill: {name}")
        return catalog[name]

    async def read_resource(
        skill_name: str, path: str, start_line: int = 1, max_lines: int = 150
    ) -> str:
        if start_line < 1 or not 1 <= max_lines <= 300:
            raise ValueError("start_line 必须为正数，max_lines 必须在 1～300 之间")
        resource = lookup(skill_name).resource_path(path)
        if resource.stat().st_size > 256 * 1024:
            raise ValueError("文本资源超过 256 KiB，请拆分资源")
        lines = (await run_blocking(resource.read_text, encoding="utf-8")).splitlines()
        selected = lines[start_line - 1 : start_line - 1 + max_lines]
        text = "\n".join(f"{index}: {line}" for index, line in enumerate(selected, start_line))
        return f"Skill={skill_name}, resource={path}, total_lines={len(lines)}\n{text}"

    def script_request(skill_name: str, arguments: list[str]) -> RunProcess:
        if len(arguments) > 64 or any(
            not isinstance(arg, str) or len(arg) > 4096 for arg in arguments
        ):
            raise ValueError("脚本参数最多 64 项，每项必须是长度不超过 4096 的字符串")
        script = lookup(skill_name).entrypoint()
        relative = script.relative_to(root).as_posix()
        return RunProcess(
            argv=("python", "-I", "-B", f"{DockerSandbox.SKILLS_MOUNT_DIR}/{relative}", *arguments),
            workdir=".",
            timeout=60,
        )

    registry.add_tool(
        Tool(
            "read_skill_resource",
            "按需读取已注册 Skill 的文本参考资料、示例或脚本",
            read_resource,
            parallel_safe=True,
            capabilities=frozenset({Capability.PROMPT}),
        )
    )
    if any(skill.metadata.get("entrypoint") for skill in skills):
        registry.add_tool(
            Tool(
                "run_skill_script",
                "在 Docker 中运行 Skill 声明的固定 Python 脚本入口，参数为字符串数组",
                script_request,
                request_builder=script_request,
                capabilities=frozenset(
                    {Capability.FILE_READ, Capability.FILE_WRITE, Capability.PROCESS}
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "skill_name": {"type": "string"},
                        "arguments": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["skill_name", "arguments"],
                    "additionalProperties": False,
                },
            )
        )
