"""Docker 进程后端。共享文件挂载与进程隔离是两个独立的边界。"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from uuid import uuid4

from loguru import logger

from xclaw.core.execution import ProcessPlan, RunProcess
from xclaw.sandbox.base import Sandbox, SandboxResult
from xclaw.sandbox.policy import ExecutionPolicy
from xclaw.sandbox.process import capture_process


class DockerSandbox(Sandbox):
    name = "docker"
    MOUNT_DIR = "/workspace"
    SKILLS_MOUNT_DIR = "/opt/xclaw-skills"

    def __init__(
        self,
        *,
        image: str = "xclaw-sandbox:latest",
        workspace: str | None = None,
        container_name: str | None = None,
        read_only: bool = False,
    ):
        super().__init__(workspace)
        self.image = image
        self.container_name = container_name or f"xclaw-sandbox-{os.getpid()}-{uuid4().hex[:8]}"
        self.read_only = read_only
        self._container_id = ""
        self._lifecycle_lock = asyncio.Lock()
        self._execution_lock = asyncio.Lock()
        self._active_task: asyncio.Task | None = None
        self._closed = False
        self._unhealthy = False
        self._owner = uuid4().hex
        self._starting = False
        self.skills_root: Path | None = None

    def configure_skills(self, root: Path) -> None:
        root = root.resolve()
        if not root.is_dir() or root == Path(self.workspace):
            raise ValueError("Skill 根目录必须是独立的已有目录")
        if self._container_id or self._closed:
            raise RuntimeError("必须在 Docker 后端启动前配置 Skill 目录")
        self.skills_root = root

    async def _cli(self, args: list[str], *, timeout=30, output_limit=8000) -> SandboxResult:
        return await capture_process(["docker", *args], timeout=timeout, output_limit=output_limit)

    async def _remove_container(self) -> None:
        """失败时保留容器标识并禁止复用，不声称已终止容器内进程。"""
        if not self._container_id:
            return
        if self._starting:
            owner = await self._cli(
                [
                    "inspect",
                    "--format",
                    '{{ index .Config.Labels "xclaw.owner" }}',
                    self._container_id,
                ]
            )
            if not owner.ok and "No such" in owner.stderr:
                self._container_id = ""
                self._starting = False
                return
            if not owner.ok or owner.stdout.strip() != self._owner:
                self._unhealthy = True
                raise RuntimeError("启动清理无法确认容器归属，未删除容器")
        result = await self._cli(["rm", "-f", self._container_id])
        if not result.ok and "No such container" not in result.stderr:
            self._unhealthy = True
            raise RuntimeError(f"Docker 容器清理失败: {result.stderr}")
        self._container_id = ""
        self._unhealthy = False
        self._starting = False

    async def start(self) -> None:
        async with self._lifecycle_lock:
            if self._closed:
                raise RuntimeError("Docker 后端已关闭")
            if self._unhealthy:
                raise RuntimeError("Docker 后端清理未完成，拒绝继续执行")
            if self._container_id:
                return
            mount = f"{self.workspace}:{self.MOUNT_DIR}"
            if self.read_only:
                mount += ":ro"
            args = [
                "run",
                "-d",
                "--name",
                self.container_name,
                "--label",
                f"xclaw.owner={self._owner}",
                "--init",
                "--memory",
                "1g",
                "--cpus",
                "1",
                "--pids-limit",
                "128",
                "--network",
                "none",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges",
                "-v",
                mount,
                "-w",
                self.MOUNT_DIR,
                self.image,
                "sleep",
                "infinity",
            ]
            if self.skills_root:
                mounts = ["-v", f"{self.skills_root}:{self.SKILLS_MOUNT_DIR}:ro"]
                try:
                    relative = self.skills_root.relative_to(Path(self.workspace))
                except ValueError:
                    pass
                else:
                    # 同一目录若也处于可写 workspace，覆盖其原路径为只读挂载。
                    mounts.extend(
                        ["-v", f"{self.skills_root}:{self.MOUNT_DIR}/{relative.as_posix()}:ro"]
                    )
                args[1:1] = mounts
            # 启动返回前取消也可以通过唯一名称清理本后端创建的容器。
            self._container_id = self.container_name
            self._starting = True
            try:
                result = await self._cli(args)
                if not result.ok:
                    raise RuntimeError(f"Docker 容器启动失败: {result.stderr}")
                self._container_id = result.stdout.strip() or self.container_name
                self._starting = False
                logger.info(f"Docker 后端启动: {self._container_id[:12]}")
            except FileNotFoundError:
                # CLI 未能启动，没有分配容器，不需要发起删除命令。
                self._container_id = ""
                self._starting = False
                raise
            except BaseException:
                try:
                    await asyncio.shield(self._remove_container())
                except Exception:
                    self._unhealthy = True
                raise

    async def stop(self) -> None:
        self._closed = True
        active = self._active_task
        if active and active is not asyncio.current_task():
            active.cancel()
            await asyncio.gather(active, return_exceptions=True)
        async with self._execution_lock:
            async with self._lifecycle_lock:
                await self._remove_container()

    async def execute(self, tool, arguments: dict) -> str:
        # 禁止通过继承 BaseSandbox.execute 在宿主机调用高风险 Python 函数。
        return "❌ 请通过 SandboxRouter 授权并提交进程请求"

    def _container_workdir(self, workdir: str) -> str:
        host_path = self.resolve_workspace_path(workdir)
        relative = host_path.relative_to(Path(self.workspace))
        return self.MOUNT_DIR + ("/" + relative.as_posix() if relative.parts else "")

    async def run_process(self, request: RunProcess) -> SandboxResult:
        # 先检查路径，再启动容器；无效调用不分配资源。
        try:
            workdir = self._container_workdir(request.workdir)
        except ValueError as exc:
            return SandboxResult(exit_code=-1, stderr=str(exc))
        async with self._execution_lock:
            self._active_task = asyncio.current_task()
            try:
                await self.start()
                command = (
                    list(request.argv) if request.shell is None else ["sh", "-c", request.shell]
                )
                result = await self._cli(
                    ["exec", "-w", workdir, self._container_id, *command],
                    timeout=request.timeout,
                    output_limit=request.output_limit,
                )
                if result.timed_out:
                    try:
                        async with self._lifecycle_lock:
                            await self._remove_container()
                        result.stderr += f"\n命令超时（>{request.timeout}s），容器已销毁"
                    except Exception as exc:
                        result.stderr += f"\n命令超时，{exc}；无法确认容器内进程已停止"
                return result
            except asyncio.CancelledError:
                try:
                    async with self._lifecycle_lock:
                        await asyncio.shield(self._remove_container())
                except Exception as exc:
                    self._unhealthy = True
                    logger.error(f"取消执行后的容器清理失败: {exc}")
                raise
            except Exception as exc:
                return SandboxResult(exit_code=-1, stderr=f"Docker 执行失败: {exc}")
            finally:
                self._active_task = None

    async def run_command(
        self, command: str, *, timeout: int = 30, workdir: str = "."
    ) -> SandboxResult:
        """兼容旧 API，实际使用相同的进程后端。"""
        return await self.run_process(RunProcess(shell=command, timeout=timeout, workdir=workdir))

    async def execute_plan(self, plan: ProcessPlan, policy: ExecutionPolicy) -> str:
        outputs = []
        for step in plan.steps:
            result = await self.run_process(policy.limit_process(step))
            marker = "\n...（输出已截断）" if result.truncated else ""
            if plan.style == "git":
                if not result.ok:
                    return f"❌ Git 命令失败 (exit={result.exit_code}):\n{result.stderr}{marker}"
                outputs.append(result.stdout.strip() or "✅ 命令执行成功（无输出）")
                if marker:
                    outputs[-1] += marker
            else:
                outputs.append(
                    f"exit_code: {result.exit_code}\n--- stdout ---\n{result.stdout}"
                    f"\n--- stderr ---\n{result.stderr}{marker}"
                )
            if not result.ok:
                break
        return "\n".join(outputs)
