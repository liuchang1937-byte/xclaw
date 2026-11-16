"""
配置管理 - 统一的配置加载和验证

支持：
- YAML 配置文件
- 环境变量覆盖
- 配置校验
- 多 Provider 配置
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import yaml
from loguru import logger
from pydantic import BaseModel, Field, model_validator

from xclaw.core.execution import ExecutionMode
from xclaw.core.scheduling import SchedulingConfig
from xclaw.mcp.config import MCPServerConfig
from xclaw.sandbox.policy import ExecutionPolicy
from xclaw.server.schedule_config import SchedulerConfig


class ProviderConfig(BaseModel):
    """LLM Provider 配置"""

    name: str = "openai"  # Provider 名称
    api_key: str = ""  # API 密钥
    base_url: str = "https://api.openai.com/v1"  # API 地址
    model: str = "gpt-4o"  # 默认模型
    context_window: int = Field(default=32768, gt=0)  # 模型输入+输出的最大上下文
    auxiliary_thinking: bool | None = False


class IMConfig(BaseModel):
    """IM 平台配置"""

    platform: str = "feishu"  # "feishu"、"qq"、"weixin" 或 "none"

    # 飞书配置
    feishu_mode: str = "websocket"  # "webhook" | "websocket" | "http"
    feishu_app_id: str = ""
    feishu_app_secret: str = ""
    feishu_verification_token: str = ""
    feishu_encrypt_key: str = ""
    feishu_webhook_url: str = ""

    # QQ 配置（QQ 开放平台只提供 AppID + AppSecret，Token 自动获取）
    qq_bot_app_id: str = ""
    qq_bot_app_secret: str = ""
    qq_mode: Literal["websocket", "bridge"] = "websocket"
    qq_bridge_host: str = "127.0.0.1"
    qq_bridge_token: str = ""
    qq_bridge_send_command: list[str] = Field(default_factory=list)

    weixin_state_file: Path = Field(default_factory=lambda: Path.home() / ".xclaw/weixin.json")

    @property
    def supports_scheduled_feishu(self) -> bool:
        return (
            self.platform == "feishu" and self.feishu_mode == "websocket"
            and not self.feishu_webhook_url
            and bool(self.feishu_app_id and self.feishu_app_secret)
        )


class AgentConfig(BaseModel):
    """Agent 配置"""

    system_prompt: str = ""  # 自定义系统提示词（覆盖默认）
    temperature: float = 0.7
    max_tokens: int = Field(default=4096, gt=0)
    max_tool_rounds: int = 10
    trace_dir: str | None = "logs/traces"
    checkpoint_dir: str | None = ".xclaw/checkpoints"
    context_strategy: Literal["summary", "truncate"] = "summary"
    isolate_workspace: bool = False
    task_workspace_dir: str = ".xclaw/tasks"
    tool_scheduling: SchedulingConfig = Field(default_factory=SchedulingConfig)
    auto_memory: bool = True
    enable_context_compression: bool = True
    compression_trigger_ratio: float = Field(default=0.85, gt=0, le=1, allow_inf_nan=False)
    compression_safety_margin: int = Field(default=1024, ge=0)
    compression_keep_recent: int = Field(default=6, ge=1)
    docker_image: str = "xclaw-sandbox:latest"
    workspace_dir: str = "."
    skills_dir: str | None = None
    execution_mode: ExecutionMode = ExecutionMode.EXECUTE
    allow_file_delete: bool = False
    execution_timeout: float = Field(default=120, gt=0, allow_inf_nan=False)
    execution_output_limit: int = Field(default=8000, gt=0)
    read_paths: list[str] = Field(default_factory=lambda: ["**"])
    write_paths: list[str] = Field(default_factory=lambda: ["**"])
    denied_paths: list[str] = Field(
        default_factory=lambda: [
            ".env",
            ".env.*",
            "*.pem",
            "*.key",
            ".ssh/**",
            ".aws/**",
        ]
    )

    def build_execution_policy(self) -> ExecutionPolicy:
        return ExecutionPolicy(
            mode=self.execution_mode,
            allow_delete=self.allow_file_delete,
            max_timeout=self.execution_timeout,
            output_limit=self.execution_output_limit,
            read_paths=tuple(self.read_paths),
            write_paths=tuple(self.write_paths),
            denied_paths=tuple(self.denied_paths),
        )


class ServerConfig(BaseModel):
    """HTTP Server 配置"""

    host: str = "0.0.0.0"
    port: int = 8080
    enable_cors: bool = True


class LogConfig(BaseModel):
    """日志配置"""

    level: str = "INFO"  # DEBUG / INFO / WARNING / ERROR
    file: str = "logs/xclaw.log"  # 日志文件路径
    rotation: str = "10 MB"  # 日志轮转大小
    retention: str = "7 days"  # 日志保留时间


class Config(BaseModel):
    """XClaw 全局配置"""

    # Provider 配置
    providers: list[ProviderConfig] = Field(default_factory=lambda: [ProviderConfig()])

    # 默认使用的 Provider
    default_provider: str = "openai"

    # Agent 配置
    agent: AgentConfig = Field(default_factory=AgentConfig)

    # IM 配置
    im: IMConfig = Field(default_factory=IMConfig)

    # Server 配置
    server: ServerConfig = Field(default_factory=ServerConfig)

    # 日志配置
    log: LogConfig = Field(default_factory=LogConfig)

    mcp_servers: list[MCPServerConfig] = Field(default_factory=list)
    scheduler: SchedulerConfig = Field(default_factory=SchedulerConfig)

    @model_validator(mode="after")
    def check_scheduled_feishu(self):
        if not self.im.supports_scheduled_feishu and any(
            job.enabled and job.feishu_chat_id for job in self.scheduler.jobs
        ):
            raise ValueError("定时任务配置 feishu_chat_id 需要启用飞书应用机器人的 WebSocket 模式")
        return self

    @classmethod
    def from_yaml(cls, path: str) -> Config:
        """从 YAML 文件加载配置"""
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return cls(**data)

    @classmethod
    def from_env(cls) -> Config:
        """从环境变量构建配置

        优先级：环境变量 > 默认值

        环境变量映射：
        - XCLAW_PROVIDER_API_KEY → providers[0].api_key
        - XCLAW_PROVIDER_BASE_URL → providers[0].base_url
        - XCLAW_PROVIDER_MODEL → providers[0].model
        - XCLAW_IM_PLATFORM → im.platform
        - XCLAW_FEISHU_APP_ID → im.feishu_app_id
        - XCLAW_FEISHU_APP_SECRET → im.feishu_app_secret
        - XCLAW_QQ_BOT_APP_ID → im.qq_bot_app_id
        - XCLAW_QQ_BOT_TOKEN → im.qq_bot_token
        - XCLAW_SERVER_PORT → server.port
        """
        config = cls()

        # Provider
        api_key = os.getenv("XCLAW_PROVIDER_API_KEY")
        if api_key:
            config.providers[0].api_key = api_key

        base_url = os.getenv("XCLAW_PROVIDER_BASE_URL")
        if base_url:
            config.providers[0].base_url = base_url

        model = os.getenv("XCLAW_PROVIDER_MODEL")
        if model:
            config.providers[0].model = model

        # IM
        im_platform = os.getenv("XCLAW_IM_PLATFORM")
        if im_platform:
            config.im.platform = im_platform

        feishu_app_id = os.getenv("XCLAW_FEISHU_APP_ID")
        if feishu_app_id:
            config.im.feishu_app_id = feishu_app_id

        feishu_app_secret = os.getenv("XCLAW_FEISHU_APP_SECRET")
        if feishu_app_secret:
            config.im.feishu_app_secret = feishu_app_secret

        qq_app_id = os.getenv("XCLAW_QQ_BOT_APP_ID")
        if qq_app_id:
            config.im.qq_bot_app_id = qq_app_id

        qq_token = os.getenv("XCLAW_QQ_BOT_TOKEN")
        if qq_token:
            config.im.qq_bot_token = qq_token

        # Server
        server_port = os.getenv("XCLAW_SERVER_PORT")
        if server_port:
            config.server.port = int(server_port)

        return config


def load_config(config_path: str | None = None) -> Config:
    """加载配置

    优先级：指定的配置文件 > 默认配置文件 > 环境变量

    Args:
        config_path: 配置文件路径

    Returns:
        Config: 配置对象
    """
    # 1. 尝试从指定路径加载
    if config_path:
        path = Path(config_path)
        if path.exists():
            logger.info(f"从 {config_path} 加载配置")
            return Config.from_yaml(str(path))
        else:
            logger.warning(f"配置文件不存在: {config_path}")

    # 2. 尝试从默认路径加载
    default_paths = [
        Path("config.yaml"),
        Path("config.yml"),
        Path("xclaw.yaml"),
        Path.home() / ".xclaw" / "config.yaml",
    ]

    for path in default_paths:
        if path.exists():
            logger.info(f"从 {path} 加载配置")
            return Config.from_yaml(str(path))

    # 3. 使用环境变量 + 默认值
    logger.info("未找到配置文件，使用环境变量和默认值")
    return Config.from_env()
