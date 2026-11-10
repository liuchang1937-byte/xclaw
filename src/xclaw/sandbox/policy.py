"""权限策略与执行位置解耦。注册的 Python 工具是可信代码。"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path

from xclaw.core.execution import Capability, ExecutionMode, RunProcess


@dataclass(frozen=True)
class ExecutionPolicy:
    mode: ExecutionMode = ExecutionMode.EXECUTE
    allow_delete: bool = False
    max_timeout: float = 120
    output_limit: int = 8000
    read_paths: tuple[str, ...] = ("**",)
    write_paths: tuple[str, ...] = ("**",)
    denied_paths: tuple[str, ...] = (".env", ".env.*", "*.pem", "*.key", ".ssh/**", ".aws/**")

    def __post_init__(self) -> None:
        object.__setattr__(self, "mode", ExecutionMode(self.mode))
        if not math.isfinite(self.max_timeout) or self.max_timeout <= 0 or self.output_limit <= 0:
            raise ValueError("策略超时和输出上限必须为正数")

    @property
    def capabilities(self) -> frozenset[Capability]:
        allowed = {Capability.PROMPT, Capability.FILE_READ, Capability.GIT_READ}
        if self.mode != ExecutionMode.VIEW:
            allowed.add(Capability.FILE_WRITE)
            if self.allow_delete:
                allowed.add(Capability.FILE_DELETE)
        if self.mode == ExecutionMode.EXECUTE:
            allowed.add(Capability.PROCESS)
            allowed.add(Capability.MCP)
        return frozenset(allowed)

    def allows(self, required: frozenset[Capability] | None) -> bool:
        return required is not None and required <= self.capabilities

    def authorize(self, required: frozenset[Capability] | None) -> None:
        if not self.allows(required):
            missing = (
                "未声明能力"
                if required is None
                else ", ".join(sorted(cap.value for cap in required - self.capabilities))
            )
            raise PermissionError(f"{self.mode.value} 模式拒绝执行：{missing}")

    def check_path(self, path: Path, workspace: Path, *, write: bool = False) -> None:
        # 校验实际目标，包含符号链接跳转后的目标。
        try:
            relative = path.resolve().relative_to(workspace.resolve()).as_posix()
        except ValueError as exc:
            raise PermissionError(f"路径超出工作区: {path}") from exc
        # 每一层目录都检查，避免扫描嵌套的 .ssh/.aws 或被禁用目录。
        parts = Path(relative).parts
        candidates = ["/".join(parts[index:]) for index in range(len(parts))]
        candidates.extend(part for part in parts)
        if any(
            fnmatchcase(candidate.casefold(), pattern.casefold())
            for pattern in self.denied_paths
            for candidate in candidates
        ):
            raise PermissionError(f"策略拒绝访问路径: {relative}")
        if write and ".git" in (part.casefold() for part in parts):
            raise PermissionError("文件工具禁止写入 .git 元数据，请使用 Git 工具")
        scopes = self.write_paths if write else self.read_paths
        if not any(fnmatchcase(relative.casefold(), pattern.casefold()) for pattern in scopes):
            raise PermissionError(f"路径不在允许范围内: {relative}")

    def limit_process(self, request: RunProcess) -> RunProcess:
        from dataclasses import replace

        return replace(
            request,
            timeout=min(request.timeout, self.max_timeout),
            output_limit=min(request.output_limit, self.output_limit),
        )

    def authorize_process(self, required: frozenset[Capability], request: RunProcess) -> None:
        self.authorize(required)
        if Capability.PROCESS in required:
            return
        # GIT_READ 只允许固定的查看操作，不能借标签提交任意进程请求。
        prefix = (
            "git",
            "--no-pager",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "core.fsmonitor=false",
        )
        if Capability.GIT_READ not in required or request.shell is not None:
            raise PermissionError("请求缺少任意进程执行权限")
        if request.argv[: len(prefix)] != prefix:
            raise PermissionError("只读 Git 请求必须禁用 hooks、pager 和 fsmonitor")
        args = request.argv[len(prefix) :]
        allowed = args in (
            ("--no-optional-locks", "status", "--short", "--branch"),
            ("diff", "--no-ext-diff", "--no-textconv"),
            ("diff", "--no-ext-diff", "--no-textconv", "--staged"),
            ("branch", "-a"),
        )
        if len(args) in (4, 5) and args[0] == "log" and re.fullmatch(r"-[1-9][0-9]?|-100", args[1]):
            allowed = args[2:] in (
                ("--no-show-signature", "--decorate"),
                ("--no-show-signature", "--oneline", "--decorate"),
            )
        if not allowed:
            raise PermissionError("只读 Git 策略拒绝此命令")
