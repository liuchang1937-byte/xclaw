"""Skill 发现、资源范围、脚本权限及 Agent 工具链接入测试。"""

import json
import sys
from pathlib import Path

import pytest

from xclaw.core.skill import SkillLoader, register_skills_as_tools
from xclaw.core.tool import ToolRegistry
from xclaw.sandbox import DockerSandbox, ExecutionPolicy, SandboxRouter
from xclaw.sandbox.process import capture_process

SKILLS = Path(__file__).resolve().parents[1] / "skills"


def write_skill(root, name="demo", body="instructions", entrypoint=None):
    folder = root / name
    folder.mkdir(parents=True, exist_ok=True)
    metadata = f"metadata:\n  entrypoint: {entrypoint}\n" if entrypoint else ""
    (folder / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: A test skill\n{metadata}---\n{body}",
        encoding="utf-8",
    )
    return folder


def test_discovery_supports_legacy_and_directory_and_loads_body_lazily(tmp_path):
    folder = write_skill(tmp_path)
    (tmp_path / "old.md").write_text(
        "---\nname: old\ndescription: Old skill\n---\nold body", encoding="utf-8"
    )
    catalog = {skill.name: skill for skill in SkillLoader(str(tmp_path)).load_all()}
    assert set(catalog) == {"demo", "old"}
    assert catalog["demo"]._prompt_template == ""
    (folder / "SKILL.md").write_text(
        "---\nname: demo\ndescription: A test skill\n---\nchanged body", encoding="utf-8"
    )
    assert catalog["demo"].prompt_template == "changed body"


def test_directory_skill_has_priority_and_invalid_metadata_is_skipped(tmp_path):
    write_skill(tmp_path)
    (tmp_path / "demo.md").write_text(
        "---\nname: demo\ndescription: Duplicate\n---\nlegacy", encoding="utf-8"
    )
    (tmp_path / "invalid.md").write_text("---\n- list\n---\nbody", encoding="utf-8")
    broken = write_skill(tmp_path, "wrong")
    (broken / "SKILL.md").write_text(
        "---\nname: mismatch\ndescription: Wrong name\n---\nbody", encoding="utf-8"
    )
    catalog = SkillLoader(str(tmp_path)).load_all()
    assert len(catalog) == 1
    assert Path(catalog[0].source_file).parent.name == "demo"


def test_lazy_entrypoint_read_cannot_change_its_original_resource_root(tmp_path):
    root = tmp_path / "skills"
    write_skill(root)
    skill = SkillLoader(str(root)).load_all()[0]
    outside = tmp_path / "outside"
    outside.mkdir()
    entry = outside / "SKILL.md"
    entry.write_text("---\nname: demo\ndescription: other\n---\nother content", encoding="utf-8")
    skill.source_file = str(entry)
    with pytest.raises(ValueError, match="原始资源目录"):
        _ = skill.prompt_template


async def test_resource_read_is_scoped_to_skill_root(tmp_path):
    folder = write_skill(tmp_path)
    (folder / "reference.md").write_text("first\nsecond\nthird", encoding="utf-8")
    registry = ToolRegistry()
    register_skills_as_tools(registry, str(tmp_path))
    result = await registry.execute(
        "read_skill_resource",
        json.dumps({"skill_name": "demo", "path": "reference.md", "start_line": 2, "max_lines": 1}),
    )
    assert "2: second" in result and "3: third" not in result
    result = await registry.execute(
        "read_skill_resource", '{"skill_name":"demo","path":"../secret.txt"}'
    )
    assert "相对路径" in result


async def test_view_mode_can_load_skill_but_cannot_execute_script(tmp_path):
    registry = ToolRegistry()
    registry.set_sandbox_router(SandboxRouter(str(tmp_path), policy=ExecutionPolicy(mode="view")))
    register_skills_as_tools(registry, str(SKILLS))
    assert "word" in registry.list_tools()
    assert "run_skill_script" not in registry.list_tools()
    instructions = await registry.execute("word", '{"user_input":"查看文档"}')
    assert "run_skill_script" in instructions and "不可用" in instructions
    result = await registry.execute(
        "run_skill_script", '{"skill_name":"word","arguments":["inspect","x.docx"]}'
    )
    assert "拒绝" in result


class RecordingDocker(DockerSandbox):
    def __init__(self, workspace):
        super().__init__(workspace=str(workspace))
        self.requests = []

    async def run_process(self, request):
        self.requests.append(request)
        from xclaw.sandbox.base import SandboxResult

        return SandboxResult(stdout="script-result")


async def test_script_request_uses_fixed_entrypoint_and_literal_arguments(tmp_path):
    registry = ToolRegistry()
    docker = RecordingDocker(tmp_path)
    registry.set_sandbox_router(SandboxRouter(str(tmp_path), docker=docker))
    register_skills_as_tools(registry, str(SKILLS))
    arguments = [
        "replace",
        "inputs/a.docx",
        "--old",
        "$(touch injected)",
        "--new",
        "text",
        "--output",
        "outputs/a.docx",
    ]
    result = await registry.execute(
        "run_skill_script", json.dumps({"skill_name": "word", "arguments": arguments})
    )
    assert "script-result" in result
    request = docker.requests[0]
    assert request.argv[:4] == ("python", "-I", "-B", "/opt/xclaw-skills/word/scripts/word_ops.py")
    assert list(request.argv[4:]) == arguments
    assert request.shell is None
    assert docker.skills_root == SKILLS
    result = await registry.execute("run_skill_script", '{"skill_name":"word","arguments":[123]}')
    assert "字符串" in result
    assert len(docker.requests) == 1


async def test_entrypoint_cannot_escape_skill_root(tmp_path):
    root = tmp_path / "skills"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    folder = write_skill(root, entrypoint="scripts/../../outside.py")
    (folder / "scripts").mkdir()
    registry = ToolRegistry()
    registry.set_sandbox_router(SandboxRouter(str(workspace)))
    register_skills_as_tools(registry, str(root))
    result = await registry.execute("run_skill_script", '{"skill_name":"demo","arguments":[]}')
    assert "相对路径" in result


async def test_native_cli_execution_through_skill_request_forwards_results(tmp_path):
    pytest.importorskip("docx")

    # 测试替身显式在隔离临时目录运行本机 CLI；生产后端仍只使用 Docker。
    class LocalTestDocker(DockerSandbox):
        async def run_process(self, request):
            relative = request.argv[3].removeprefix(self.SKILLS_MOUNT_DIR + "/")
            return await capture_process(
                [sys.executable, "-I", "-B", str(SKILLS / relative), *request.argv[4:]],
                timeout=request.timeout,
                output_limit=request.output_limit,
                cwd=str(tmp_path),
            )

    (tmp_path / "spec.json").write_text('{"title":"Skill result","blocks":[]}', encoding="utf-8")
    registry = ToolRegistry()
    registry.set_sandbox_router(
        SandboxRouter(str(tmp_path), docker=LocalTestDocker(workspace=str(tmp_path)))
    )
    register_skills_as_tools(registry, str(SKILLS))
    result = await registry.execute(
        "run_skill_script",
        json.dumps(
            {
                "skill_name": "word",
                "arguments": ["create", "--spec", "spec.json", "--output", "result.docx"],
            }
        ),
    )
    assert '"ok": true' in result
    assert (tmp_path / "result.docx").is_file()


@pytest.mark.parametrize(
    "skill,suffix,spec",
    [
        ("word", ".docx", {"title": "Integration report", "blocks": []}),
        ("excel", ".xlsx", {"sheets": [{"name": "Data", "rows": [["ID"], ["001"]]}]}),
    ],
)
async def test_agent_can_activate_office_skill_and_execute_its_script(
    tmp_path, skill, suffix, spec
):
    pytest.importorskip("docx" if skill == "word" else "openpyxl")
    from xclaw.ai.provider import BaseProvider
    from xclaw.ai.types import ChatResponse, FunctionCall, StreamChunk, ToolCall
    from xclaw.coding import CodingAgent

    class TestDocker(DockerSandbox):
        async def run_process(self, request):
            relative = request.argv[3].removeprefix(self.SKILLS_MOUNT_DIR + "/")
            return await capture_process(
                [sys.executable, "-I", "-B", str(SKILLS / relative), *request.argv[4:]],
                timeout=request.timeout,
                output_limit=request.output_limit,
                cwd=str(tmp_path),
            )

    class TestProvider(BaseProvider):
        def __init__(self):
            self.turn = 0

        async def chat(self, messages, tools=None, **kwargs):
            self.turn += 1
            if self.turn == 1:
                name, arguments = skill, {"user_input": "Create office file"}
            elif self.turn == 2:
                name, arguments = (
                    "read_skill_resource",
                    {"skill_name": skill, "path": "reference.md"},
                )
            elif self.turn == 3:
                name, arguments = (
                    "run_skill_script",
                    {
                        "skill_name": skill,
                        "arguments": [
                            "create",
                            "--spec",
                            "spec.json",
                            "--output",
                            "result" + suffix,
                        ],
                    },
                )
            else:
                assert any(
                    message.role == "tool" and '"ok": true' in (message.content or "")
                    for message in messages
                )
                return ChatResponse(content="Office file created")
            return ChatResponse(
                tool_calls=[
                    ToolCall(
                        id=f"call-{self.turn}",
                        function=FunctionCall(name=name, arguments=json.dumps(arguments)),
                    )
                ]
            )

        async def chat_stream(self, *args, **kwargs):
            yield StreamChunk(delta_content="unused")

    (tmp_path / "spec.json").write_text(json.dumps(spec), encoding="utf-8")
    agent = CodingAgent(
        TestProvider(),
        workspace_dir=str(tmp_path),
        skills_dir=str(SKILLS),
        sandbox=TestDocker(workspace=str(tmp_path)),
        enable_context_compression=True,
    )
    assert await agent.run("Create office file") == "Office file created"
    assert (tmp_path / ("result" + suffix)).is_file()
    assert [message.role for message in agent.client.get_history()] == ["user", "assistant"]
    await agent.sandbox_router.stop()
