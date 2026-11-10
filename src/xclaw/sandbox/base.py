"""
沙箱抽象 - Agent 执行环境的隔离层

设计理念：
- 统一接口：命令执行 + 工作区快照/回滚
- BaseSandbox/LocalSandbox：旧 API 兼容类，主链路使用 HostBackend
- DockerSandbox：命令在容器内执行（进程/副作用隔离），文件通过 bind mount 共享

对应清单中的 "Agentic Sandbox / Docker 沙箱：容器隔离执行、宿主机保护"。
"""

from __future__ import annotations

import inspect
import os
import shutil
import tarfile
import tempfile
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loguru import logger

from xclaw.core.tool import RiskLevel


@dataclass
class SandboxResult:
    """命令执行结果"""

    exit_code: int = 0
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    truncated: bool = False

    @property
    def ok(self) -> bool:
        """命令是否执行成功"""
        return self.exit_code == 0 and not self.timed_out


class Sandbox(ABC):
    """所有风险执行环境必须遵循的统一接口。

    任何隔离方式都实现这套接口，工具层无需关心底层执行位置。
    """

    # 沙箱名称（用于日志和标识）
    name: str = "sandbox"

    def __init__(self, workspace: str | None = None):
        """
        Args:
            workspace: 工作区目录（宿主机视角）。None 表示使用当前目录。
        """
        self.workspace = str(Path(workspace or os.getcwd()).resolve())

    @abstractmethod
    async def start(self) -> None:
        """启动沙箱（Docker 容器在此创建，本地沙箱为空操作）"""
        ...

    @abstractmethod
    async def stop(self) -> None:
        """停止沙箱（释放资源）"""
        ...

    @abstractmethod
    async def execute(self, tool: Any, arguments: dict[str, Any]) -> str:
        """在该环境的风险策略下执行工具。"""
        ...

    @abstractmethod
    async def run_command(
        self, command: str, *, timeout: int = 30, workdir: str = "."
    ) -> SandboxResult:
        """在沙箱内执行一条 shell 命令

        Args:
            command: 要执行的命令
            timeout: 超时时间（秒）
            workdir: 工作目录（相对 workspace）

        Returns:
            SandboxResult: exit_code + stdout + stderr
        """
        ...

    # ─── 快照 / 回滚（默认实现：对 workspace 目录做 tar 备份）───────

    def snapshot(self) -> str:
        """创建工作区快照，返回快照 ID（tar.gz 文件路径）

        快照打包整个 workspace 目录，用于执行危险操作前的备份。

        Returns:
            str: 快照文件路径（传给 rollback 使用）
        """
        fd, path = tempfile.mkstemp(prefix="xclaw-snap-", suffix=".tar.gz")
        os.close(fd)

        with tarfile.open(path, "w:gz") as tar:
            tar.add(self.workspace, arcname=".")

        logger.info(f"[{self.name}] 工作区已快照: {path}")
        return path

    def rollback(self, snapshot_id: str) -> None:
        """回滚工作区到指定快照

        清空当前 workspace 内容，并从快照文件恢复。

        Args:
            snapshot_id: snapshot() 返回的快照文件路径
        """
        if not os.path.exists(snapshot_id):
            logger.error(f"快照不存在: {snapshot_id}")
            return

        # 清空当前工作区内容
        for item in os.listdir(self.workspace):
            p = os.path.join(self.workspace, item)
            if os.path.isdir(p):
                shutil.rmtree(p, ignore_errors=True)
            else:
                try:
                    os.remove(p)
                except OSError:
                    pass

        # 从快照恢复
        with tarfile.open(snapshot_id, "r:gz") as tar:
            try:
                # Python 3.12+ 推荐显式指定 filter，防止路径穿越
                tar.extractall(self.workspace, filter="data")
            except TypeError:
                # Python 3.11 及更早没有 filter 参数
                tar.extractall(self.workspace)

        logger.info(f"[{self.name}] 工作区已回滚到快照: {snapshot_id}")

    def resolve_workspace_path(self, value: str) -> Path:
        """解析并校验路径必须位于当前 workspace。"""
        root = Path(self.workspace).resolve()
        candidate = Path(value).expanduser()
        if not candidate.is_absolute():
            candidate = root / candidate
        resolved = candidate.resolve()
        try:
            resolved.relative_to(root)
        except ValueError as exc:
            raise ValueError(f"路径超出工作区: {value}") from exc
        return resolved

    def prepare_arguments(self, tool: Any, arguments: dict[str, Any]) -> dict[str, Any]:
        """复制参数，并把工具声明的路径字段规范化为工作区绝对路径。"""
        prepared = dict(arguments)
        for parameter in tool.path_parameters:
            value = prepared.get(parameter)
            if value is None:
                function_parameter = inspect.signature(tool.func).parameters.get(parameter)
                if (
                    function_parameter is not None
                    and function_parameter.default is not inspect.Parameter.empty
                ):
                    value = function_parameter.default
            if isinstance(value, str):
                prepared[parameter] = str(self.resolve_workspace_path(value))
        return prepared


class BaseSandbox(Sandbox):
    """Baseline 完全信任模式，只承载无副作用的只读工具。

    名称中的 Base 表示 baseline mode；真正的抽象接口是 Sandbox。
    """

    name = "base"
    allowed_risks = frozenset({RiskLevel.READ_ONLY})

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def execute(self, tool: Any, arguments: dict[str, Any]) -> str:
        if tool.risk_level not in self.allowed_risks:
            return f"❌ {self.name} 模式拒绝执行 {tool.risk_level.value} 工具: {tool.name}"
        try:
            prepared = self.prepare_arguments(tool, arguments)
        except ValueError as exc:
            return f"❌ {exc}"
        return await tool.execute(**prepared)

    async def run_command(
        self, command: str, *, timeout: int = 30, workdir: str = "."
    ) -> SandboxResult:
        """Base 模式不允许执行任意命令。"""
        return SandboxResult(exit_code=-1, stderr="BaseSandbox 只允许只读工具")
