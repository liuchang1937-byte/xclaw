"""LocalSandbox - 受限的宿主机工作区写入环境。"""

from __future__ import annotations

from xclaw.core.tool import RiskLevel
from xclaw.sandbox.base import BaseSandbox, SandboxResult


class LocalSandbox(BaseSandbox):
    """允许只读与工作区写入，但拒绝任意代码执行。

    文件路径由 BaseSandbox 规范化并限制在 workspace 内。这个模式降低
    Agent 误操作风险，但不等同于操作系统级隔离。
    """

    name = "local"
    allowed_risks = frozenset({RiskLevel.READ_ONLY, RiskLevel.WORKSPACE_WRITE})

    async def run_command(
        self, command: str, *, timeout: int = 30, workdir: str = "."
    ) -> SandboxResult:
        """任意代码执行必须路由到 DockerSandbox。"""
        return SandboxResult(
            exit_code=-1,
            stderr="LocalSandbox 拒绝任意命令，请使用 DockerSandbox",
        )
