"""执行能力与请求类型；不依赖具体后端。"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum


class Capability(str, Enum):
    PROMPT = "prompt"
    FILE_READ = "file_read"
    FILE_WRITE = "file_write"
    FILE_DELETE = "file_delete"
    PROCESS = "process"
    GIT_READ = "git_read"
    MCP = "mcp"


class ExecutionMode(str, Enum):
    VIEW = "view"
    EDIT = "edit"
    EXECUTE = "execute"


@dataclass(frozen=True)
class RunProcess:
    """argv 与 shell 二选一。请求由可信工具实现构建，不能由模型指定后端。"""

    argv: tuple[str, ...] = ()
    shell: str | None = None
    workdir: str = "."
    timeout: float = 30
    output_limit: int = 8000

    def __post_init__(self) -> None:
        if bool(self.argv) == (self.shell is not None):
            raise ValueError("必须且只能指定 argv 或 shell")
        if not math.isfinite(self.timeout) or self.timeout <= 0 or self.output_limit <= 0:
            raise ValueError("超时和输出上限必须为正数")
        if any(not isinstance(arg, str) or "\x00" in arg for arg in self.argv):
            raise ValueError("进程参数必须为不含 NUL 的字符串")


@dataclass(frozen=True)
class ProcessPlan:
    """例如 git add → commit；前一步失败时不执行后续步骤。"""

    steps: tuple[RunProcess, ...]
    style: str = "process"

    def __post_init__(self) -> None:
        if not self.steps:
            raise ValueError("进程计划不能为空")
