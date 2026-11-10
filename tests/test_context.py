"""上下文预算与滚动压缩测试。"""

import pytest

from xclaw.ai.context import ContextCompressor, estimate_tools_tokens
from xclaw.ai.provider import BaseProvider
from xclaw.ai.types import (
    ChatMessage,
    ChatResponse,
    FunctionCall,
    StreamChunk,
    ToolCall,
    ToolDefinition,
)


class SummaryProvider(BaseProvider):
    def __init__(self, summary: str = "任务摘要"):
        self.summary = summary

    async def chat(self, messages, tools=None, *, model, temperature=0.7, max_tokens=4096):
        return ChatResponse(content=self.summary)

    async def chat_stream(self, messages, tools=None, *, model, temperature=0.7, max_tokens=4096):
        yield StreamChunk(delta_content=self.summary)


def test_request_budget_counts_current_messages_and_tools():
    compressor = ContextCompressor(
        SummaryProvider(),
        context_window=200,
        max_output_tokens=20,
        safety_margin=10,
        trigger_ratio=1.0,
    )
    history = [ChatMessage(role="user", content="短历史")]
    current = [ChatMessage(role="user", content="新请求" * 100)]
    tools = [
        ToolDefinition(
            name="read_file",
            description="读取文件" * 30,
            parameters={"type": "object", "properties": {}},
        )
    ]

    assert estimate_tools_tokens(tools) > 0
    assert compressor.should_compress(history, current, tools=tools)


@pytest.mark.asyncio
async def test_compress_keeps_history_when_summary_fails():
    compressor = ContextCompressor(SummaryProvider(summary=""), keep_recent=2)
    history = [
        ChatMessage(role="user", content="旧问题" * 50),
        ChatMessage(role="assistant", content="旧回答" * 50),
        ChatMessage(role="user", content="新问题"),
        ChatMessage(role="assistant", content="新回答"),
    ]

    result = await compressor.compress(history)

    assert result == history


@pytest.mark.asyncio
async def test_compress_replaces_old_messages_with_summary():
    compressor = ContextCompressor(SummaryProvider(), keep_recent=2)
    history = [
        ChatMessage(role="user", content="旧问题" * 50),
        ChatMessage(role="assistant", content="旧回答" * 50),
        ChatMessage(role="user", content="新问题"),
        ChatMessage(role="assistant", content="新回答"),
    ]

    result = await compressor.compress(history)

    assert result[0].content is not None
    assert "任务摘要" in result[0].content
    assert result[1:] == history[2:]


def test_cut_point_keeps_tool_call_with_its_results():
    compressor = ContextCompressor(SummaryProvider(), keep_recent=2)
    history = [
        ChatMessage(role="user", content="修复问题"),
        ChatMessage(
            role="assistant",
            tool_calls=[
                ToolCall(
                    id="call-1",
                    function=FunctionCall(name="read_file", arguments="{}"),
                )
            ],
        ),
        ChatMessage(role="tool", content="文件内容", tool_call_id="call-1"),
        ChatMessage(role="assistant", content="处理中"),
    ]

    assert compressor._find_cut_point(history, whole_turns=False) == 1
    assert compressor._find_cut_point(history) == 0
