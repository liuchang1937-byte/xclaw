"""先授权，再构建请求和选择后端。权限等级不再决定执行位置。"""

from __future__ import annotations

from pathlib import Path

from loguru import logger

from xclaw.core.concurrency import ReadWriteGate
from xclaw.core.execution import Capability, ProcessPlan, RunProcess
from xclaw.core.tool import RiskLevel
from xclaw.sandbox.docker import DockerSandbox
from xclaw.sandbox.host import HostBackend
from xclaw.sandbox.policy import ExecutionPolicy


class SandboxRouter:
    def __init__(self, workspace: str, *, host=None, docker=None, policy=None):
        self.workspace = Path(workspace).resolve()
        self.policy = policy if policy is not None else ExecutionPolicy()
        self.host = host if host is not None else HostBackend(str(self.workspace))
        self.docker = (
            docker
            if docker is not None
            else DockerSandbox(
                workspace=str(self.workspace),
                read_only=Capability.PROCESS not in self.policy.capabilities,
            )
        )
        if Path(self.host.workspace).resolve() != self.workspace or (
            Path(self.docker.workspace).resolve() != self.workspace
        ):
            raise ValueError("执行后端必须使用同一工作区")
        if Capability.PROCESS not in self.policy.capabilities:
            if self.docker._container_id and not self.docker.read_only:
                raise ValueError("受限模式不能复用可写容器")
            self.docker.read_only = True
        self.last_route: str | None = None
        self._execution_gate = ReadWriteGate()

    def select(self, risk_level: RiskLevel):
        """旧调用者的兼容入口；主流程按请求类型选择后端。"""
        return self.docker if risk_level == RiskLevel.CODE_EXECUTION else self.host

    async def execute(self, tool, arguments: dict) -> str:
        try:
            self.policy.authorize(tool.capabilities)
            prepared = tool.bind_arguments(arguments)
            if Capability.MCP in tool.capabilities:
                # MCP 使用独立连接，不伪装为本地文件工具或 Docker 进程请求。
                self.last_route = "mcp"
                async with self._execution_gate.hold(shared=False):
                    return await tool.execute(**prepared)
            write = bool({Capability.FILE_WRITE, Capability.FILE_DELETE} & tool.capabilities)
            for parameter in tool.path_parameters:
                path = self.host.resolve_workspace_path(prepared[parameter])
                # A process working directory is a location, not a file write target.
                # PROCESS authorizes its side effects; file globs cannot constrain shell internals.
                is_workdir = tool.request_builder is not None and parameter == "workdir"
                self.policy.check_path(path, self.workspace, write=write and not is_workdir)
                prepared[parameter] = str(path)
            if tool.request_builder is not None:
                request = tool.request_builder(**prepared)
                if isinstance(request, RunProcess):
                    request = ProcessPlan((request,))
                if not isinstance(request, ProcessPlan):
                    raise ValueError("进程工具必须构建 RunProcess 或 ProcessPlan")
                if not ({Capability.PROCESS, Capability.GIT_READ} & tool.capabilities):
                    raise PermissionError("进程请求缺少执行能力声明")
                for step in request.steps:
                    self.policy.authorize_process(tool.capabilities, step)
                    path = self.host.resolve_workspace_path(step.workdir)
                    self.policy.check_path(path, self.workspace, write=False)
                async with self._execution_gate.hold(shared=False):
                    self.last_route = self.docker.name
                    logger.info(f"执行路由: tool={tool.name}, backend={self.last_route}")
                    return await self.docker.execute_plan(request, self.policy)
            if {Capability.PROCESS, Capability.GIT_READ} & tool.capabilities:
                raise PermissionError("进程工具未提供请求构建器，拒绝在宿主机调用函数")
            self.last_route = self.host.name
            async with self._execution_gate.hold(shared=tool.supports_parallel):
                lease = getattr(self.docker, "workspace_lease", None)
                if lease and lease.exists():
                    raise PermissionError("工作区容器尚未清理，暂时无法从宿主机访问文件")
                return await self.host.execute(tool, prepared)
        except (ValueError, TypeError, PermissionError) as exc:
            return f"❌ {exc}"

    async def stop(self) -> None:
        await self.docker.stop()
        await self.host.stop()
