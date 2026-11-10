"""Git 工具构建 argv 请求，执行统一交给后端，不在宿主机启动 Git。"""

from __future__ import annotations

from xclaw.core.execution import Capability, ProcessPlan, RunProcess
from xclaw.core.tool import RiskLevel, Tool, ToolRegistry


def _git(args: list[str], path: str) -> RunProcess:
    return RunProcess(
        argv=(
            "git",
            "--no-pager",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "core.fsmonitor=false",
            *args,
        ),
        workdir=path,
    )


def git_status(path: str = ".") -> ProcessPlan:
    return ProcessPlan(
        (_git(["--no-optional-locks", "status", "--short", "--branch"], path),), "git"
    )


def git_diff(path: str = ".", staged: bool = False) -> ProcessPlan:
    args = ["diff", "--no-ext-diff", "--no-textconv"]
    if staged:
        args.append("--staged")
    return ProcessPlan((_git(args, path),), "git")


def git_commit(message: str, path: str = ".", add_all: bool = False) -> ProcessPlan:
    if not message.strip():
        raise ValueError("提交信息不能为空")
    steps = [_git(["add", "-A"], path)] if add_all else []
    steps.append(_git(["commit", "-m", message], path))
    return ProcessPlan(tuple(steps), "git")


def git_log(path: str = ".", count: int = 10, oneline: bool = True) -> ProcessPlan:
    if not 1 <= count <= 100:
        raise ValueError("count 必须在 1 到 100 之间")
    args = ["log", f"-{count}", "--no-show-signature"]
    if oneline:
        args.append("--oneline")
    args.append("--decorate")
    return ProcessPlan((_git(args, path),), "git")


def git_branch(path: str = ".") -> ProcessPlan:
    return ProcessPlan((_git(["branch", "-a"], path),), "git")


def git_checkout(branch: str, path: str = ".", create: bool = False) -> ProcessPlan:
    if not branch.strip() or branch.startswith("-") or "\x00" in branch:
        raise ValueError("branch 必须是非空分支名，不能以 '-' 开头")
    args = ["checkout", "-b", branch] if create else ["checkout", branch]
    return ProcessPlan((_git(args, path),), "git")


def register_git_tools(registry: ToolRegistry) -> None:
    definitions = [
        (git_status, "查看 Git 仓库状态", False),
        (git_diff, "查看文件差异，可选择暂存区", False),
        (git_log, "查看提交历史，count 最大为 100", False),
        (git_branch, "查看本地和远程分支", False),
        (git_commit, "创建提交，可选先 add 所有更改；容器内执行且禁用 hooks", True),
        (git_checkout, "切换或创建分支；容器内执行且禁用 hooks", True),
    ]
    for func, description, write in definitions:
        registry.add_tool(
            Tool(
                name=func.__name__,
                description=description,
                func=func,
                request_builder=func,
                risk_level=RiskLevel.CODE_EXECUTION if write else RiskLevel.READ_ONLY,
                capabilities=frozenset(
                    {Capability.FILE_READ, Capability.FILE_WRITE, Capability.PROCESS}
                )
                if write
                else frozenset({Capability.FILE_READ, Capability.GIT_READ}),
                path_parameters=("path",),
            )
        )
