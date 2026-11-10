"""
AI 交互层 - 对应 pi-mono 中的 pi-ai 模块

职责：
- 封装 LLM 调用（支持 OpenAI / Anthropic 兼容接口）
- 流式输出管理
- 多 Provider 适配
- 请求重试和错误处理
"""

from xclaw.ai.client import AIClient
from xclaw.ai.context import ContextBudgetExceededError, ContextProtocolError
from xclaw.ai.provider import OpenAIProvider, ProviderRegistry
from xclaw.ai.types import (
    ChatMessage,
    ChatResponse,
    StreamChunk,
    ToolCall,
    ToolResult,
)

__all__ = [
    "AIClient",
    "ContextBudgetExceededError",
    "ContextProtocolError",
    "OpenAIProvider",
    "ProviderRegistry",
    "ChatMessage",
    "ChatResponse",
    "ToolCall",
    "ToolResult",
    "StreamChunk",
]
