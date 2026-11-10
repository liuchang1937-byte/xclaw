"""Docker 生命周期与进程协议测试；默认不依赖 Docker daemon。"""

import asyncio
import os
import sys

import pytest

from xclaw.core.execution import RunProcess
from xclaw.core.tool import RiskLevel, Tool
from xclaw.sandbox import DockerSandbox
from xclaw.sandbox.base import SandboxResult
from xclaw.sandbox.process import capture_process


class SimulatedDocker(DockerSandbox):
    def __init__(self, workspace):
        super().__init__(workspace=str(workspace))
        self.commands = []
        self.exec_result = SandboxResult(stdout="output")
        self.block = False
        self.executing = asyncio.Event()
        self.rm_fails = False

    async def _cli(self, args, **kwargs):
        self.commands.append(args)
        await asyncio.sleep(0)
        if args[0] == "run":
            return SandboxResult(stdout="owned-container")
        if args[0] == "inspect":
            return SandboxResult(stdout=self._owner)
        if args[0] == "exec":
            self.executing.set()
            if self.block:
                await asyncio.Event().wait()
            return self.exec_result
        if args[0] == "rm" and self.rm_fails:
            return SandboxResult(exit_code=1, stderr="daemon unavailable")
        return SandboxResult()


async def test_concurrent_start_is_idempotent(tmp_path):
    docker = SimulatedDocker(tmp_path)
    await asyncio.gather(docker.start(), docker.start(), docker.start())
    assert sum(args[0] == "run" for args in docker.commands) == 1
    await docker.stop()
    await docker.stop()
    assert sum(args[0] == "rm" for args in docker.commands) == 1


async def test_invalid_workdir_does_not_start_container(tmp_path):
    docker = SimulatedDocker(tmp_path)
    result = await docker.run_process(RunProcess(argv=("echo", "hello"), workdir="../"))
    assert not result.ok
    assert docker.commands == []


async def test_native_argv_and_read_only_mount(tmp_path):
    docker = SimulatedDocker(tmp_path)
    docker.read_only = True
    result = await docker.run_process(RunProcess(argv=("echo", "literal; $(whoami)")))
    assert result.ok
    run = next(args for args in docker.commands if args[0] == "run")
    assert run[run.index("-v") + 1].endswith(":ro")
    command = next(args for args in docker.commands if args[0] == "exec")
    assert command[-2:] == ["echo", "literal; $(whoami)"]
    assert "sh" not in command
    await docker.stop()


async def test_skill_directory_has_read_only_alias_and_workspace_mask(tmp_path):
    root = tmp_path / "skills"
    root.mkdir()
    docker = SimulatedDocker(tmp_path)
    docker.configure_skills(root)
    await docker.start()
    command = next(args for args in docker.commands if args[0] == "run")
    mounts = [command[index + 1] for index, value in enumerate(command) if value == "-v"]
    assert f"{root}:/opt/xclaw-skills:ro" in mounts
    assert f"{root}:/workspace/skills:ro" in mounts
    with pytest.raises(RuntimeError):
        docker.configure_skills(root)
    await docker.stop()


async def test_timeout_destroys_container_and_next_call_restarts(tmp_path):
    docker = SimulatedDocker(tmp_path)
    docker.exec_result = SandboxResult(exit_code=-1, timed_out=True)
    result = await docker.run_command("sleep 999", timeout=1)
    assert result.timed_out
    assert "容器已销毁" in result.stderr
    assert not docker._container_id
    assert ["rm", "-f", "owned-container"] in docker.commands
    docker.exec_result = SandboxResult(stdout="healthy")
    assert (await docker.run_command("echo healthy")).ok
    assert sum(args[0] == "run" for args in docker.commands) == 2
    await docker.stop()


async def test_failed_cleanup_blocks_reuse(tmp_path):
    docker = SimulatedDocker(tmp_path)
    docker.rm_fails = True
    docker.exec_result = SandboxResult(exit_code=-1, timed_out=True)
    result = await docker.run_command("sleep 999")
    assert "清理失败" in result.stderr
    before = sum(args[0] == "exec" for args in docker.commands)
    result = await docker.run_command("echo unsafe")
    assert not result.ok
    assert sum(args[0] == "exec" for args in docker.commands) == before
    docker.rm_fails = False
    await docker.stop()


async def test_cancel_removes_container(tmp_path):
    docker = SimulatedDocker(tmp_path)
    docker.block = True
    task = asyncio.create_task(docker.run_command("sleep 999"))
    await docker.executing.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not docker._container_id
    assert ["rm", "-f", "owned-container"] in docker.commands


async def test_stop_cancels_active_execution_and_rejects_new_calls(tmp_path):
    docker = SimulatedDocker(tmp_path)
    docker.block = True
    task = asyncio.create_task(docker.run_command("sleep 999"))
    await docker.executing.wait()
    await docker.stop()
    assert task.cancelled()
    assert not docker._container_id
    result = await docker.run_command("echo closed")
    assert "已关闭" in result.stderr


async def test_docker_never_calls_host_function(tmp_path):
    calls = []
    tool = Tool("unsafe", "", lambda: calls.append("host"), risk_level=RiskLevel.CODE_EXECUTION)
    result = await DockerSandbox(workspace=str(tmp_path)).execute(tool, {})
    assert "授权" in result
    assert calls == []


async def test_missing_docker_does_not_fall_back_to_host(tmp_path, monkeypatch):
    docker = DockerSandbox(workspace=str(tmp_path))

    async def missing(*args, **kwargs):
        raise FileNotFoundError("docker missing")

    monkeypatch.setattr(docker, "_cli", missing)
    result = await docker.run_command("echo dangerous")
    assert not result.ok
    assert "docker missing" in result.stderr


async def test_start_failure_does_not_delete_unowned_container(tmp_path):
    docker = SimulatedDocker(tmp_path)

    async def conflict(args, **kwargs):
        docker.commands.append(args)
        if args[0] == "run":
            return SandboxResult(exit_code=1, stderr="name conflict")
        return SandboxResult(stdout="another-owner")

    docker._cli = conflict
    result = await docker.run_command("echo unsafe")
    assert not result.ok
    assert not any(args[0] == "rm" for args in docker.commands)
    assert docker._unhealthy


async def test_capture_drains_large_output_with_bounded_buffers():
    result = await capture_process(
        [
            sys.executable,
            "-c",
            "import sys; sys.stdout.write('x'*100000); sys.stderr.write('y'*100000)",
        ],
        timeout=10,
        output_limit=100,
    )
    assert result.ok
    assert result.stdout == "x" * 100
    assert result.stderr == "y" * 100
    assert result.truncated


async def test_capture_timeout_preserves_partial_output():
    result = await capture_process(
        [sys.executable, "-c", "import time; print('started', flush=True); time.sleep(30)"],
        timeout=1,
        output_limit=100,
    )
    assert result.timed_out
    assert not result.ok
    assert "started" in result.stdout


@pytest.mark.skipif(os.getenv("XCLAW_DOCKER_TESTS") != "1", reason="需要 Docker 和已构建沙箱镜像")
async def test_real_container_timeout_removes_running_job(tmp_path):
    docker = DockerSandbox(workspace=str(tmp_path))
    try:
        result = await docker.run_process(RunProcess(shell="sleep 30; touch marker", timeout=1))
        assert result.timed_out
        assert not docker._container_id
        assert not (tmp_path / "marker").exists()
    finally:
        await docker.stop()
