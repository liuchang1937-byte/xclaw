"""
Coding Agent - 专业的编程助手 Agent

特点：
- 预配置了文件操作和 Git 工具
- 内置编码专用的系统提示词
- 可以分析项目架构、编写代码、调试问题

对应 pi-mono 中的 pi-coding-agent 实现。

使用示例：
    from xclaw.coding import CodingAgent

    agent = CodingAgent(provider, model="gpt-4o")

    # 分析项目架构
    response = await agent.run("分析这个项目的整体架构")

    # 编写代码
    response = await agent.run("实现一个用户认证模块")

    # 调试问题
    response = await agent.run("src/api.py 的第 42 行有个 bug，帮我修复")
"""

from __future__ import annotations

from pathlib import Path

from loguru import logger

from xclaw.ai.provider import BaseProvider
from xclaw.coding.bash_tool import register_bash_tool
from xclaw.coding.excel_tool import register_excel_tool
from xclaw.coding.file_tools import register_file_tools
from xclaw.coding.git_tools import register_git_tools
from xclaw.core.agent import Agent
from xclaw.core.execution import ExecutionMode
from xclaw.core.scheduling import SchedulingConfig
from xclaw.core.tool import ToolRegistry
from xclaw.mom.bus import EventBus
from xclaw.sandbox import DockerSandbox, ExecutionPolicy, SandboxRouter


class CodingAgent(Agent):
    """编码 Agent - 专业的编程助手

    预配置的能力：
    - 文件读写和搜索
    - Git 版本控制
    - 代码分析和生成
    - 项目架构理解
    """

    # Coding Agent 的系统提示词
    SYSTEM_PROMPT = """你是一个专业的编程助手（Coding Agent），名为 XClaw。

## 你的核心能力
1. **代码编写**：编写高质量、可维护的代码
2. **代码分析**：理解现有代码的架构和逻辑
3. **问题调试**：定位和修复 bug
4. **项目理解**：分析项目结构和模块关系
5. **文件操作**：读取、写入、搜索文件
6. **版本控制**：使用 Git 管理代码变更

## 工作原则
- 先理解再动手：修改代码前先阅读相关文件
- 保持一致性：遵循项目现有的代码风格和架构模式
- 信息安全：不泄露密钥、密码等敏感信息
- 注释清晰：关键逻辑要添加中文注释
- 最小改动：尽量用最小的修改解决问题
- 创建简单 Excel 数据表时优先调用 create_excel 直接生成真实 .xlsx，无需 Docker。
  复杂 Excel 编辑或 Word 文件使用对应 Skill 和 run_skill_script，
  不需要搜索或修改 Agent 框架源码。检查工具输出与生成的文件。
- 当 deliver_file 工具可用且用户要求交付文件时，必须调用它提交实际生成的最终文件；
  路径、代码和 Markdown 表格不能代替附件。不要把内部工具标记写进回复正文。

## 响应格式
- 如果是编码任务，先说明思路，再给出代码
- 如果涉及文件修改，说明修改的原因和影响
- 如果遇到不确定的情况，主动询问澄清
"""

    @property
    def system_prompt(self) -> str:
        return getattr(self, "_system_prompt_override", "") or self.SYSTEM_PROMPT

    @property
    def agent_name(self) -> str:
        return "CodingAgent"

    def __init__(
        self,
        provider: BaseProvider,
        *,
        model: str = "gpt-4o",
        temperature: float = 0.3,  # 编码任务使用较低温度
        max_tokens: int = 8192,  # 编码任务需要更多 token
        max_tool_rounds: int = 10,
        system_prompt: str = "",
        auto_memory: bool = False,
        enable_context_compression: bool = False,
        context_window: int = 32768,
        compression_trigger_ratio: float = 0.85,
        compression_safety_margin: int = 1024,
        compression_keep_recent: int = 6,
        workspace_dir: str = ".",
        skills_dir: str | None = None,
        sandbox: DockerSandbox | None = None,
        docker_image: str = "xclaw-sandbox:latest",
        execution_policy: ExecutionPolicy | None = None,
        event_bus: EventBus | None = None,  # 事件总线（可选，用于生命周期事件）
        register_default_tools: bool = True,
        tool_scheduling: SchedulingConfig | None = None,
        trace_dir: str | None = None,
        context_strategy: str = "summary",
        checkpoint_dir: str | None = None,
    ):
        """
        初始化 Coding Agent

        Args:
            provider: LLM Provider
            model: 模型名称
            temperature: 生成温度（编码任务建议 0.1-0.3）
            max_tokens: 最大 token 数
            workspace_dir: 工作目录
            register_default_tools: 是否自动注册默认工具
        """
        self._system_prompt_override = system_prompt
        private_registry = ToolRegistry()
        super().__init__(
            provider=provider,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            event_bus=event_bus,
            auto_memory=auto_memory,
            tool_registry=private_registry,
            tool_scheduling=tool_scheduling,
            checkpoint_dir=checkpoint_dir,
        )
        self.MAX_TOOL_ROUNDS = max_tool_rounds
        if context_strategy not in {"summary", "truncate"}:
            raise ValueError("context_strategy 必须是 summary 或 truncate")
        self.client.context_strategy = context_strategy
        self.trace_dir = trace_dir

        if enable_context_compression:
            self.client.enable_compression(
                context_window=context_window,
                max_output_tokens=max_tokens,
                trigger_ratio=compression_trigger_ratio,
                safety_margin=compression_safety_margin,
                keep_recent=compression_keep_recent,
            )

        self.workspace_dir = workspace_dir
        self.memory_scope = "project:" + str(Path(workspace_dir).resolve())
        package_skills = Path(__file__).resolve().parents[1] / "bundled_skills"
        default_skills = (
            package_skills
            if package_skills.is_dir()
            else Path(__file__).resolve().parents[3] / "skills"
        )
        self.skills_dir = skills_dir or str(default_skills)
        self.sandbox = sandbox
        self.docker_image = docker_image
        self.execution_policy = execution_policy or ExecutionPolicy()

        # 每个 Agent 持有独立的权限策略与执行后端。
        self._setup_sandbox()

        # 注册默认工具
        if register_default_tools:
            self._register_default_tools()

        logger.info(
            f"CodingAgent 已初始化: model={model}, "
            f"workspace={workspace_dir}, mode={self.execution_policy.mode.value}, "
            "backends=host/docker"
        )

    def _setup_sandbox(self) -> None:
        """创建权限策略与 Host/Docker 后端。"""
        docker = self.sandbox if isinstance(self.sandbox, DockerSandbox) else None
        if docker is None:
            docker = DockerSandbox(
                workspace=self.workspace_dir,
                image=self.docker_image,
                read_only=self.execution_policy.mode != ExecutionMode.EXECUTE,
            )
        self.sandbox_router = SandboxRouter(
            self.workspace_dir,
            docker=docker,
            policy=self.execution_policy,
        )
        self.tool_registry.set_sandbox_router(self.sandbox_router)

    def _register_default_tools(self) -> None:
        """注册编码所需的默认工具集 + Markdown Skill"""
        register_file_tools(
            self.tool_registry,
            workspace=self.workspace_dir,
            policy=self.execution_policy,
        )
        register_git_tools(self.tool_registry)
        register_bash_tool(self.tool_registry)
        register_excel_tool(self.tool_registry)

        # 加载 skills/ 目录下的 Markdown Skill 文件
        from xclaw.core.skill import register_skills_as_tools

        skill_count = len(register_skills_as_tools(self.tool_registry, self.skills_dir))

        logger.info(
            f"CodingAgent 已注册 {len(self.tool_registry)} 个工具 (含 {skill_count} 个 Skill)"
        )
