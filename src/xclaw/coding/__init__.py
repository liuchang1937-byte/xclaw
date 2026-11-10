"""
编码 Agent 模块 - 对应 pi-mono 中的 pi-coding-agent

职责：
- 提供编码相关的工具（文件读写、代码搜索、Git 操作）
- 实现 CodingAgent（预配置了编码工具的 Agent 子类）
- 代码质量检查和自动修复

设计理念：
- 工具即能力：所有编码能力都通过工具暴露
- 安全第一：危险操作（如删文件）需要确认
- 上下文感知：理解项目结构和代码关系
"""

from xclaw.coding.coding_agent import CodingAgent
from xclaw.coding.bash_tool import register_bash_tool
from xclaw.coding.file_tools import register_file_tools, set_workspace_root
from xclaw.coding.git_tools import register_git_tools

__all__ = [
    "CodingAgent",
    "register_bash_tool",
    "register_file_tools",
    "register_git_tools",
    "set_workspace_root",
]
