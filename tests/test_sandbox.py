"""三级风险 Sandbox 路由测试。"""

import pytest

from xclaw.coding.bash_tool import bash, register_bash_tool
from xclaw.coding.file_tools import register_file_tools
from xclaw.core.execution import RunProcess
from xclaw.core.tool import RiskLevel, Tool, ToolRegistry
from xclaw.sandbox import DockerSandbox, HostBackend, LocalSandbox, SandboxRouter
from xclaw.sandbox.base import SandboxResult


class FakeDockerSandbox(DockerSandbox):
    """不启动真实 Docker，只记录收到的命令。"""

    def __init__(self, workspace: str):
        super().__init__(workspace=workspace)
        self.commands: list[tuple[str, str]] = []

    async def run_process(self, request: RunProcess) -> SandboxResult:
        self.resolve_workspace_path(request.workdir)
        self.commands.append((request.shell, request.workdir))
        return SandboxResult(exit_code=0, stdout="docker-result")

    async def stop(self) -> None:
        return None


def test_read_and_write_share_host_backend(tmp_path):
    router = SandboxRouter(str(tmp_path), docker=FakeDockerSandbox(str(tmp_path)))

    assert isinstance(router.select(RiskLevel.READ_ONLY), HostBackend)
    assert router.select(RiskLevel.READ_ONLY) is router.select(RiskLevel.WORKSPACE_WRITE)
    assert isinstance(router.select(RiskLevel.CODE_EXECUTION), DockerSandbox)


@pytest.mark.asyncio
async def test_read_and_write_tools_are_routed(tmp_path):
    (tmp_path / "a.txt").write_text("hello", encoding="utf-8")
    registry = ToolRegistry()
    router = SandboxRouter(str(tmp_path), docker=FakeDockerSandbox(str(tmp_path)))
    registry.set_sandbox_router(router)
    register_file_tools(registry)

    result = await registry.execute("read_file", '{"path":"a.txt"}')
    assert "hello" in result
    assert router.last_route == "host"

    await registry.execute("write_file", '{"path":"b.txt","content":"new"}')
    assert (tmp_path / "b.txt").read_text(encoding="utf-8") == "new"
    assert router.last_route == "host"


@pytest.mark.asyncio
async def test_workspace_escape_is_blocked(tmp_path):
    registry = ToolRegistry()
    registry.set_sandbox_router(SandboxRouter(str(tmp_path)))
    register_file_tools(registry)

    result = await registry.execute("read_file", '{"path":"../secret.txt"}')
    assert "路径超出工作区" in result


@pytest.mark.asyncio
async def test_local_rejects_code_execution(tmp_path):
    local = LocalSandbox(str(tmp_path))
    tool = Tool(
        name="execute",
        description="执行代码",
        func=lambda: "should-not-run",
        risk_level=RiskLevel.CODE_EXECUTION,
    )

    result = await local.execute(tool, {})
    assert "拒绝执行" in result


@pytest.mark.asyncio
async def test_bash_is_bound_to_docker(tmp_path):
    docker = FakeDockerSandbox(str(tmp_path))
    registry = ToolRegistry()
    router = SandboxRouter(str(tmp_path), docker=docker)
    registry.set_sandbox_router(router)
    register_bash_tool(registry, docker)

    result = await registry.execute("bash", '{"command":"pytest"}')

    assert "docker-result" in result
    assert docker.commands == [("pytest", str(tmp_path.resolve()))]
    assert router.last_route == "docker"


@pytest.mark.asyncio
async def test_bash_dangerous_command_blocked(tmp_path):
    docker = FakeDockerSandbox(str(tmp_path))
    result = await bash("rm -rf /", sandbox=docker)
    assert "拦截" in result
    assert docker.commands == []


def test_snapshot_and_rollback(tmp_path):
    sandbox = LocalSandbox(workspace=str(tmp_path))
    (tmp_path / "a.txt").write_text("hello", encoding="utf-8")
    snapshot = sandbox.snapshot()

    (tmp_path / "a.txt").write_text("world", encoding="utf-8")
    (tmp_path / "b.txt").write_text("new", encoding="utf-8")
    sandbox.rollback(snapshot)

    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "hello"
    assert not (tmp_path / "b.txt").exists()


def test_docker_attributes(tmp_path):
    sandbox = DockerSandbox(image="python:3.11-slim", workspace=str(tmp_path))
    assert sandbox.name == "docker"
    assert sandbox.MOUNT_DIR == "/workspace"
    assert sandbox.image == "python:3.11-slim"
    assert sandbox.workspace == str(tmp_path.resolve())
