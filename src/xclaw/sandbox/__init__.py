"""
沙箱模块 - Agent 执行环境的隔离层

权限由 ExecutionPolicy 管理；HostBackend 执行可信文件/提示词工具，
DockerSandbox 执行进程请求。BaseSandbox/LocalSandbox 只保留兼容旧调用者。
"""

from __future__ import annotations

from xclaw.sandbox.base import BaseSandbox, Sandbox, SandboxResult
from xclaw.sandbox.docker import DockerSandbox
from xclaw.sandbox.host import HostBackend
from xclaw.sandbox.local import LocalSandbox
from xclaw.sandbox.policy import ExecutionPolicy
from xclaw.sandbox.router import SandboxRouter

__all__ = [
    "Sandbox",
    "BaseSandbox",
    "SandboxResult",
    "LocalSandbox",
    "DockerSandbox",
    "SandboxRouter",
    "HostBackend",
    "ExecutionPolicy",
]
