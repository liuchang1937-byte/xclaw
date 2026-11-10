"""宿主机只运行可信文件/提示词工具；进程请求由 Docker 后端执行。"""

from __future__ import annotations

from xclaw.core.execution import Capability
from xclaw.sandbox.base import Sandbox, SandboxResult


class HostBackend(Sandbox):
    name = "host"

    async def start(self):
        return None

    async def stop(self):
        return None

    async def run_command(self, command, *, timeout=30, workdir="."):
        return SandboxResult(exit_code=-1, stderr="宿主机后端禁止任意进程执行")

    async def execute(self, tool, arguments):
        if (
            tool.request_builder is not None
            or tool.capabilities is None
            or ({Capability.PROCESS, Capability.GIT_READ, Capability.MCP} & tool.capabilities)
        ):
            return "❌ 宿主机后端拒绝进程工具或未声明能力的工具"
        return await tool.execute(**arguments)
