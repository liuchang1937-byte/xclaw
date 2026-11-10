"""MCP 客户端接入：连接服务、发现工具并注册到 Agent。"""

from xclaw.mcp.client import MCPClientManager
from xclaw.mcp.config import MCPServerConfig

__all__ = ["MCPClientManager", "MCPServerConfig"]
