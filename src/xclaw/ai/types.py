"""
AI 模块的类型定义

定义消息、工具调用、响应等核心数据结构。
设计理念：简洁、明确、可扩展。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class FunctionCall(BaseModel):
    """工具/函数调用的参数定义"""

    name: str = Field(..., description="函数名")
    arguments: str = Field(default="{}", description="函数参数的 JSON 字符串")


class ToolCall(BaseModel):
    """一次工具调用"""

    id: str = Field(..., description="工具调用的唯一 ID")
    type: Literal["function"] = "function"
    function: FunctionCall = Field(..., description="调用的函数信息")
    # 流式传输的索引只用于聚合，不写入持久历史或 OpenAI 请求格式。
    index: int | None = Field(default=None, ge=0, exclude=True)


class ToolResult(BaseModel):
    """工具调用的返回结果"""

    tool_call_id: str = Field(..., description="对应的工具调用 ID")
    content: str = Field(..., description="工具返回的内容")
    name: str = Field(default="", description="工具名称")


class ChatMessage(BaseModel):
    """通用聊天消息格式"""

    role: Literal["system", "user", "assistant", "tool"] = Field(..., description="消息角色")
    content: str | None = Field(default=None, description="消息文本内容")
    name: str | None = Field(default=None, description="发送者名称（可选）")
    tool_calls: list[ToolCall] | None = Field(default=None, description="助手发起的工具调用")
    tool_call_id: str | None = Field(default=None, description="工具消息对应的调用 ID")
    reasoning_content: str | None = Field(default=None, description="供应商要求回传的推理字段")

    def to_openai_format(self) -> dict[str, Any]:
        """转换为 OpenAI 兼容的消息格式"""
        msg: dict[str, Any] = {"role": self.role}
        if self.content is not None:
            msg["content"] = self.content
        if self.name is not None:
            msg["name"] = self.name
        if self.tool_calls is not None:
            msg["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": tc.type,
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                }
                for tc in self.tool_calls
            ]
        if self.tool_call_id is not None:
            msg["tool_call_id"] = self.tool_call_id
        if self.reasoning_content is not None:
            msg["reasoning_content"] = self.reasoning_content
        return msg


class ToolDefinition(BaseModel):
    """工具定义 - 描述一个可用的工具"""

    name: str = Field(..., description="工具名称")
    description: str = Field(..., description="工具描述")
    parameters: dict[str, Any] = Field(default_factory=dict, description="工具参数的 JSON Schema")

    def to_openai_format(self) -> dict[str, Any]:
        """转换为 OpenAI 兼容的工具定义格式"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class TokenUsage(BaseModel):
    """Token 用量统计"""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class ChatResponse(BaseModel):
    """完整的聊天响应"""

    content: str | None = Field(default=None, description="模型回复的文本内容")
    tool_calls: list[ToolCall] = Field(default_factory=list, description="模型发起的工具调用")
    finish_reason: str | None = Field(
        default=None, description="结束原因: stop / tool_calls / length"
    )
    usage: TokenUsage | None = Field(default=None, description="Token 用量")
    reasoning_content: str | None = None


class StreamChunk(BaseModel):
    """流式输出的一个片段"""

    delta_content: str | None = Field(default=None, description="增量文本内容")
    delta_tool_calls: list[ToolCall] | None = Field(default=None, description="增量工具调用")
    finish_reason: str | None = Field(default=None, description="结束原因")
    delta_reasoning_content: str | None = None
    usage: TokenUsage | None = None
