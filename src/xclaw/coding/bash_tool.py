"""
bash 工具 - 通用 shell 命令执行

设计理念：
- 让 Agent 具备"运行验证"能力：跑测试、装依赖、执行脚本
- 默认带超时和输出截断，防止命令挂死或刷屏
- 返回 exit code + stdout + stderr，方便 LLM 判断成功与否

安全提示：
- bash 是"任意命令执行"能力，生产环境应配合 Docker 沙箱或命令白名单使用
- 本实现提供 timeout / 输出截断 / 危险命令拦截三个基础保护

对应 pi-mono 中 Agent 的执行环境能力。
"""

from __future__ import annotations

from loguru import logger

from xclaw.core.execution import Capability, RunProcess
from xclaw.core.tool import RiskLevel, Tool, ToolRegistry
from xclaw.sandbox import DockerSandbox

# 危险命令黑名单（命中则拦截，返回警告而不执行）
DANGEROUS_PATTERNS = [
    "rm -rf /",
    "rm -rf /*",
    "mkfs",
    "dd if=",
    ":(){ :|:& };:",  # fork bomb
    "> /dev/sda",
    "shutdown",
    "format c:",
]

# 单条命令输出最大字符数（防止刷屏）
MAX_OUTPUT_CHARS = 8000


async def bash(
    command: str,
    timeout: int = 30,
    workdir: str = ".",
    *,
    sandbox: DockerSandbox | None = None,
) -> str:
    """执行一条 shell 命令并返回输出

    Args:
        command: 要执行的 shell 命令
        timeout: 超时时间（秒），默认 30
        workdir: 执行命令的工作目录

    Returns:
        str: exit code + stdout + stderr 的组合文本
    """
    # 危险命令拦截
    for pattern in DANGEROUS_PATTERNS:
        if pattern in command:
            logger.warning(f"[bash] 拦截危险命令: {command}")
            return f"❌ 已拦截危险命令（命中模式 '{pattern}'）: {command}"

    if sandbox is None:
        return "❌ Bash 工具未绑定 DockerSandbox"

    try:
        result = await sandbox.run_command(command, timeout=timeout, workdir=workdir)
    except Exception as e:
        return f"❌ 命令执行异常: {e}"

    parts = [f"$ {command}", f"exit_code: {result.exit_code}"]
    if result.stdout:
        parts.append("--- stdout ---")
        parts.append(_truncate(result.stdout))
    if result.stderr:
        parts.append("--- stderr ---")
        parts.append(_truncate(result.stderr))

    return "\n".join(parts)


def _truncate(text: str) -> str:
    """截断过长输出"""
    if len(text) <= MAX_OUTPUT_CHARS:
        return text.rstrip()
    return text[:MAX_OUTPUT_CHARS] + f"\n... (输出过长，已截断，原 {len(text)} 字符)"


def register_bash_tool(
    registry: ToolRegistry,
    sandbox: DockerSandbox | None = None,
) -> None:
    """将 bash 工具注册到指定的工具注册中心

    Args:
        registry: 工具注册中心实例
    """

    def routed_bash(
        command: str,
        timeout: int = 30,
        workdir: str = ".",
    ) -> RunProcess:
        if not command.strip():
            raise ValueError("command 不能为空")
        for pattern in DANGEROUS_PATTERNS:
            if pattern in command:
                raise ValueError(f"已拦截危险命令（命中模式 '{pattern}'）")
        return RunProcess(shell=command, timeout=timeout, workdir=workdir)

    registry.add_tool(
        Tool(
            name="bash",
            description=(
                "在 shell 中执行一条命令并返回输出（exit code + stdout + stderr）。"
                "用于运行测试、安装依赖、执行脚本、查看环境等需要实际运行的场景。"
                "超时默认 30 秒，输出过长会自动截断。"
            ),
            func=routed_bash,
            request_builder=routed_bash,
            capabilities=frozenset(
                {
                    Capability.FILE_READ,
                    Capability.FILE_WRITE,
                    Capability.PROCESS,
                }
            ),
            parameters={
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "timeout": {"type": "integer", "default": 30},
                    "workdir": {"type": "string", "default": "."},
                },
                "required": ["command"],
            },
            risk_level=RiskLevel.CODE_EXECUTION,
            path_parameters=("workdir",),
        )
    )
    logger.info("已注册 bash 工具")
