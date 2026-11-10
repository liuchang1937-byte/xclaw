"""权限拒绝必须发生在副作用之前，策略和后端必须实际接入工具链。"""

import asyncio
import json

import pytest

from xclaw.coding.bash_tool import register_bash_tool
from xclaw.coding.file_tools import register_file_tools
from xclaw.coding.git_tools import git_commit, register_git_tools
from xclaw.core.execution import Capability, ExecutionMode, RunProcess
from xclaw.core.skill import Skill
from xclaw.core.tool import RiskLevel, Tool, ToolRegistry
from xclaw.sandbox import DockerSandbox, ExecutionPolicy, SandboxRouter
from xclaw.sandbox.base import SandboxResult


class RecordingDocker(DockerSandbox):
    def __init__(self, workspace):
        super().__init__(workspace=str(workspace))
        self.requests = []
        self.results = []

    async def run_process(self, request):
        self.requests.append(request)
        return self.results.pop(0) if self.results else SandboxResult(stdout="container-result")


def build_registry(tmp_path, policy=None):
    docker = RecordingDocker(tmp_path)
    registry = ToolRegistry()
    router = SandboxRouter(str(tmp_path), docker=docker, policy=policy)
    registry.set_sandbox_router(router)
    register_file_tools(registry)
    register_git_tools(registry)
    register_bash_tool(registry)
    return registry, docker


@pytest.mark.parametrize("mode", [ExecutionMode.VIEW, ExecutionMode.EDIT])
async def test_restricted_modes_deny_arbitrary_processes(tmp_path, mode):
    registry, docker = build_registry(tmp_path, ExecutionPolicy(mode=mode))
    assert "拒绝" in await registry.execute("bash", '{"command":"pytest"}')
    assert "拒绝" in await registry.execute("git_commit", '{"message":"test"}')
    assert docker.requests == []
    assert "bash" not in registry.list_tools()
    assert registry.get_definition("git_commit") is None
    assert docker.read_only


async def test_view_allows_fixed_git_reads_in_read_only_container(tmp_path):
    registry, docker = build_registry(tmp_path, ExecutionPolicy(mode="view"))
    assert "container-result" in await registry.execute("git_diff", "{}")
    assert docker.read_only
    request = docker.requests[0]
    assert request.shell is None
    assert "--no-ext-diff" in request.argv
    assert "--no-textconv" in request.argv


async def test_view_denies_file_write_before_side_effect(tmp_path):
    registry, _ = build_registry(tmp_path, ExecutionPolicy(mode="view"))
    result = await registry.execute("write_file", '{"path":"a.txt","content":"new"}')
    assert "拒绝" in result
    assert not (tmp_path / "a.txt").exists()


async def test_file_write_globs_do_not_block_process_workdir_but_still_limit_file_tools(tmp_path):
    registry, docker = build_registry(tmp_path, ExecutionPolicy(write_paths=("app.py",)))
    # Use structured JSON rather than shell escaping to exercise the working-directory check.
    result = await registry.execute("bash", json.dumps({"command": "python -c 'print(1)'"}))
    assert "container-result" in result
    assert docker.requests
    result = await registry.execute("write_file", json.dumps({"path": "other.py", "content": "x"}))
    assert "拒绝" in result or "允许范围" in result
    assert not (tmp_path / "other.py").exists()


async def test_delete_requires_separate_permission(tmp_path):
    (tmp_path / "a.txt").write_text("original")
    registry, _ = build_registry(tmp_path)
    assert "拒绝" in await registry.execute("delete_file", '{"path":"a.txt"}')
    assert (tmp_path / "a.txt").exists()
    registry, _ = build_registry(tmp_path, ExecutionPolicy(mode="edit", allow_delete=True))
    assert "已删除" in await registry.execute("delete_file", '{"path":"a.txt"}')


async def test_recursive_search_applies_denied_paths(tmp_path):
    (tmp_path / "visible.txt").write_text("needle")
    (tmp_path / ".env").write_text("needle_secret")
    registry, _ = build_registry(tmp_path)
    assert "拒绝" in await registry.execute("read_file", '{"path":".env"}')
    result = await registry.execute("search_in_files", '{"directory":".","query":"needle"}')
    assert "visible.txt" in result
    assert "needle_secret" not in result


async def test_custom_write_scope_and_git_metadata_protection(tmp_path):
    (tmp_path / "src").mkdir()
    registry, _ = build_registry(tmp_path, ExecutionPolicy(write_paths=("src/**",)))
    assert "允许范围" in await registry.execute(
        "write_file", '{"path":"other.txt","content":"bad"}'
    )
    assert not (tmp_path / "other.txt").exists()
    assert "已写入" in await registry.execute("write_file", '{"path":"src/a.txt","content":"ok"}')
    assert "元数据" in await registry.execute(
        "write_file", '{"path":".git/config","content":"bad"}'
    )


async def test_two_workspaces_do_not_override_each_other(tmp_path):
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    (first / "a.txt").write_text("first-content")
    (second / "a.txt").write_text("second-content")
    first_registry, _ = build_registry(first)
    second_registry, _ = build_registry(second)
    outputs = await asyncio.gather(
        first_registry.execute("read_file", '{"path":"a.txt"}'),
        second_registry.execute("read_file", '{"path":"a.txt"}'),
    )
    assert "first-content" in outputs[0]
    assert "second-content" in outputs[1]


async def test_undeclared_tool_and_process_function_fail_closed(tmp_path):
    registry, docker = build_registry(tmp_path)
    calls = []

    def func():
        calls.append("host")

    registry.add_tool(Tool("unknown", "", func))
    registry.add_tool(Tool("unsafe", "", func, risk_level=RiskLevel.CODE_EXECUTION))
    assert "未声明" in await registry.execute("unknown", "{}")
    assert "构建器" in await registry.execute("unsafe", "{}")
    assert calls == []
    assert docker.requests == []


async def test_git_label_cannot_authorize_shell(tmp_path):
    registry, docker = build_registry(tmp_path, ExecutionPolicy(mode="view"))

    def request():
        return RunProcess(shell="echo unsafe")

    registry.add_tool(
        Tool(
            "fake_git",
            "",
            request,
            request_builder=request,
            capabilities=frozenset({Capability.GIT_READ}),
        )
    )
    assert "权限" in await registry.execute("fake_git", "{}")
    assert docker.requests == []


async def test_git_add_failure_prevents_commit_and_preserves_argv(tmp_path):
    registry, docker = build_registry(tmp_path)
    docker.results = [SandboxResult(exit_code=1, stderr="add failed")]
    message = "test; $(echo injected) 'quoted'"
    result = await registry.execute("git_commit", json.dumps({"message": message, "add_all": True}))
    assert "add failed" in result
    assert len(docker.requests) == 1
    plan = git_commit(message)
    assert plan.steps[0].argv[-1] == message
    assert plan.steps[0].shell is None
    assert "core.hooksPath=/dev/null" in plan.steps[0].argv


async def test_limits_and_invalid_arguments_precede_execution(tmp_path):
    registry, docker = build_registry(tmp_path, ExecutionPolicy(max_timeout=5, output_limit=100))
    assert "container-result" in await registry.execute(
        "bash", '{"command":"pytest","timeout":999}'
    )
    assert docker.requests[0].timeout == 5
    assert docker.requests[0].output_limit == 100
    for payload in (
        "[]",
        '{"command":"pytest","timeout":"30"}',
        '{"command":"pytest","timeout":true}',
        '{"command":"pytest","backend":"host"}',
        '{"command":"pytest","workdir":"../"}',
        '{"command":"pytest","timeout":0}',
    ):
        result = await registry.execute("bash", payload)
        assert "错误" in result or "❌" in result
    assert len(docker.requests) == 1


async def test_skill_is_available_in_view_mode(tmp_path):
    registry, _ = build_registry(tmp_path, ExecutionPolicy(mode="view"))
    registry.add_tool(Skill("guide", "guide", "prompt text").to_tool())
    assert "prompt text" in await registry.execute("guide", '{"user_input":"test"}')


def test_file_schema_uses_resolved_annotations(tmp_path):
    registry, _ = build_registry(tmp_path)
    assert (
        registry.get_definition("read_file").parameters["properties"]["start_line"]["type"]
        == "integer"
    )
    assert (
        registry.get_definition("edit_file").parameters["properties"]["replace_all"]["type"]
        == "boolean"
    )


async def test_search_cannot_escape_through_symlink(tmp_path):
    outside = tmp_path / "outside"
    root = tmp_path / "root"
    outside.mkdir()
    root.mkdir()
    (outside / "secret.txt").write_text("outside-secret")
    try:
        (root / "link.txt").symlink_to(outside / "secret.txt")
    except OSError:
        pytest.skip("当前系统不允许创建符号链接")
    registry, _ = build_registry(root)
    assert "超出工作区" in await registry.execute("read_file", '{"path":"link.txt"}')
    result = await registry.execute("search_in_files", '{"directory":".","query":"outside-secret"}')
    assert "outside-secret" not in result or "未找到" in result
    assert "link.txt:" not in result


async def test_custom_denied_paths_apply_to_recursive_results(tmp_path):
    (tmp_path / "nested" / ".aws").mkdir(parents=True)
    (tmp_path / "nested" / ".aws" / "credentials").write_text("needle_private")
    (tmp_path / "hidden.txt").write_text("needle_hidden")
    (tmp_path / "public.txt").write_text("needle_public")
    registry, _ = build_registry(
        tmp_path,
        ExecutionPolicy(denied_paths=(".aws/**", "hidden.txt")),
    )
    result = await registry.execute("search_in_files", '{"directory":".","query":"needle"}')
    assert "needle_public" in result
    assert "needle_private" not in result
    assert "needle_hidden" not in result


@pytest.mark.parametrize("mode", ["view", "edit", "execute"])
def test_coding_agent_uses_policy_and_private_workspace(tmp_path, mode):
    from tests.test_context import SummaryProvider
    from xclaw.coding import CodingAgent

    agent = CodingAgent(
        SummaryProvider(),
        workspace_dir=str(tmp_path),
        execution_policy=ExecutionPolicy(mode=mode),
    )
    assert agent.sandbox_router.policy.mode.value == mode
    assert agent.sandbox_router.docker.read_only == (mode != "execute")
    assert ("bash" in agent.tool_registry.list_tools()) == (mode == "execute")
    assert agent.sandbox_router.workspace == tmp_path.resolve()


def test_server_configuration_validates_mode_and_limits():
    from pydantic import ValidationError

    from xclaw.server.config import AgentConfig

    assert AgentConfig(execution_mode="view").execution_mode == ExecutionMode.VIEW
    for kwargs in (
        {"execution_mode": "unknown"},
        {"execution_timeout": 0},
        {"execution_timeout": float("inf")},
        {"execution_output_limit": -1},
    ):
        with pytest.raises(ValidationError):
            AgentConfig(**kwargs)
