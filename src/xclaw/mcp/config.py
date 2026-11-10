"""MCP 服务配置；凭据仅在连接时从环境变量展开。"""

from __future__ import annotations

import os
import re
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, model_validator


def expand_env(value: str) -> str:
    """只展开显式 ${NAME}，缺失变量报错而不是发送原样凭据。"""

    def replace(match: re.Match) -> str:
        name = match.group(1)
        if name not in os.environ:
            raise ValueError(f"MCP 环境变量未设置: {name}")
        return os.environ[name]

    return re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", replace, value)


class MCPServerConfig(BaseModel):
    name: str = Field(pattern=r"^[A-Za-z0-9_-]{1,24}$")
    transport: Literal["stdio", "streamable-http"] = "stdio"
    enabled: bool = True
    command: str = ""
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    cwd: str | None = None
    url: str = ""
    headers: dict[str, str] = Field(default_factory=dict)
    timeout: float = Field(default=30, gt=0, allow_inf_nan=False)
    # None 表示导入服务提供的全部工具；空列表表示不导入任何工具。
    allowed_tools: list[str] | None = None

    @model_validator(mode="after")
    def validate_transport(self) -> MCPServerConfig:
        if not self.enabled:
            return self
        if self.transport == "stdio" and not self.command.strip():
            raise ValueError("stdio MCP 服务必须配置 command")
        if self.transport == "streamable-http":
            url = urlsplit(self.url)
            if url.scheme not in ("http", "https") or not url.hostname:
                raise ValueError("HTTP MCP 服务必须配置有效的 http/https URL")
        return self
