"""完整请求预算、压缩失败回滚及对话/工具消息边界的回归测试。"""

import asyncio
import json

import pytest

from xclaw.ai.client import AIClient
from xclaw.ai.context import (
    SUMMARY_PREFIX,
    TRUNCATION_MARKER,
    ContextBudgetExceededError,
    ContextCompressor,
    ContextProtocolError,
    estimate_messages_tokens,
    estimate_tokens,
    protocol_boundaries,
    split_text,
)
from xclaw.ai.provider import BaseProvider
from xclaw.ai.types import (
    ChatMessage,
    ChatResponse,
    FunctionCall,
    StreamChunk,
    ToolCall,
    ToolDefinition,
)


class RecordingProvider(BaseProvider):
    def __init__(self, summary="任务摘要", error=None):
        self.summary = summary
        self.error = error
        self.summary_requests = []
        self.requests = []
        self.stream_requests = []

    async def chat(self, messages, tools=None, *, model, temperature=0.7, max_tokens=4096):
        snapshot = [message.model_copy(deep=True) for message in messages]
        if len(messages) == 1 and (messages[0].content or "").startswith(
            ContextCompressor.SUMMARY_PROMPT
        ):
            self.summary_requests.append((snapshot, max_tokens))
            if self.error:
                raise self.error
            return ChatResponse(content=self.summary)
        self.requests.append((snapshot, tools))
        return ChatResponse(content="最终回复")

    async def chat_stream(self, messages, tools=None, *, model, temperature=0.7, max_tokens=4096):
        self.stream_requests.append(
            ([message.model_copy(deep=True) for message in messages], tools)
        )
        yield StreamChunk(delta_content="最终回复")


def client_for(provider, *, window=1024, keep=6):
    client = AIClient(provider, max_tokens=64, system_prompt="你是编程助手。")
    client.enable_compression(
        context_window=window,
        safety_margin=32,
        trigger_ratio=1.0,
        keep_recent=keep,
    )
    return client


def history_pairs(count, size=200):
    return [
        message
        for index in range(count)
        for message in (
            ChatMessage(role="user", content=f"旧任务{index}:" + "问题" * size),
            ChatMessage(role="assistant", content=f"旧回答{index}:" + "回答" * size),
        )
    ]


def tool_group(
    call_id="call-1", content="文件内容", name="read_file", arguments='{"path":"src/a.py"}'
):
    return [
        ChatMessage(
            role="assistant",
            tool_calls=[
                ToolCall(
                    id=call_id,
                    function=FunctionCall(name=name, arguments=arguments),
                )
            ],
        ),
        ChatMessage(role="tool", content=content, tool_call_id=call_id, name=name),
    ]


def assert_requests_fit(provider, compressor):
    assert len(provider.summary_requests) <= compressor.MAX_SUMMARY_CALLS
    for messages, reserved in provider.summary_requests:
        limit = int(
            (compressor.context_window - reserved - compressor.safety_margin)
            * compressor.trigger_ratio
        )
        assert estimate_messages_tokens(messages) <= limit
    for messages, tools in provider.requests + provider.stream_requests:
        assert compressor.estimate_request_tokens(messages, tools=tools) <= compressor.input_budget
        protocol_boundaries(messages)


async def test_small_request_does_not_summarize_or_change_history():
    provider = RecordingProvider()
    client = client_for(provider)
    client.set_history(history_pairs(1, size=5))
    original = client.get_history()
    await client.chat([ChatMessage(role="user", content="继续")], save_history=False)
    assert client.get_history() == original
    assert provider.summary_requests == []
    assert_requests_fit(provider, client.compressor)


async def test_long_history_compresses_and_keeps_recent_complete_turns():
    provider = RecordingProvider()
    client = client_for(provider, window=2048)
    original = history_pairs(8, size=90)
    client.set_history(original)
    await client.chat([ChatMessage(role="user", content="继续当前任务")], save_history=False)
    assert provider.summary_requests
    assert client.get_history()[0].content.startswith(SUMMARY_PREFIX)
    assert client.get_history()[-6:] == original[-6:]
    assert_requests_fit(provider, client.compressor)


async def test_large_latest_history_turn_can_be_summarized_to_fit():
    provider = RecordingProvider()
    client = client_for(provider, window=2048)
    client.set_history(history_pairs(1, size=800))
    await client.chat(
        [ChatMessage(role="user", content="新的要求原文必须保留")], save_history=False
    )
    assert len(client.get_history()) == 1
    assert client.get_history()[0].content.startswith(SUMMARY_PREFIX)
    assert provider.requests[0][0][-1].content == "新的要求原文必须保留"
    assert_requests_fit(provider, client.compressor)


@pytest.mark.parametrize(
    "summary,error", [("", None), ("   ", None), (None, RuntimeError("summary offline"))]
)
async def test_failed_summary_preserves_history_and_blocks_oversized_request(summary, error):
    provider = RecordingProvider(summary=summary, error=error)
    client = client_for(provider)
    original = history_pairs(3, size=150)
    client.set_history(original)
    client.compressor.summary = "previous-summary"
    current = [ChatMessage(role="user", content="继续")]
    with pytest.raises(ContextBudgetExceededError):
        await client.chat(current, save_history=False)
    assert client.get_history() == original
    assert current == [ChatMessage(role="user", content="继续")]
    assert client.compressor.summary == "previous-summary"
    assert len(provider.summary_requests) == 1
    assert provider.requests == []


async def test_summary_that_expands_history_is_not_installed():
    provider = RecordingProvider(summary="长" * 200)
    compressor = ContextCompressor(provider, keep_recent=2)
    original = history_pairs(2, size=2)
    assert await compressor.compress(original) == original
    assert compressor.summary == ""


@pytest.mark.parametrize("component", ["user", "system", "tools"])
async def test_irreducible_input_fails_before_any_model_call(component):
    provider = RecordingProvider()
    client = client_for(provider)
    current = [ChatMessage(role="user", content="本轮要求")]
    tools = None
    if component == "user":
        current[0].content = "用户明确要求" * 1000
    elif component == "system":
        client.system_prompt = "系统提示" * 1000
    else:
        tools = [ToolDefinition(name="large", description="工具说明" * 1000, parameters={})]
    snapshot = current[0].model_copy(deep=True)
    with pytest.raises(ContextBudgetExceededError, match="本身过大"):
        await client.chat(current, tools, save_history=False)
    assert current[0] == snapshot
    assert provider.requests == provider.summary_requests == []


async def test_large_current_tool_output_preserves_user_and_protocol():
    provider = RecordingProvider()
    client = client_for(provider)
    goal = "修复 bug，禁止改动接口；必须保留这句原始要求。"
    output = "日志开始\n" + "中间日志" * 2000 + "\nTraceback: failing assertion"
    original_tool = tool_group(content=output)
    current = [ChatMessage(role="user", content=goal), *original_tool]
    await client.chat(current, save_history=False)
    assert current[0].content == goal
    assert TRUNCATION_MARKER in current[-1].content
    assert current[-1].content.startswith("日志开始")
    assert current[-1].content.endswith("failing assertion")
    assert current[-1].tool_call_id == original_tool[-1].tool_call_id
    assert current[1].tool_calls == original_tool[0].tool_calls
    assert original_tool[-1].content == output
    assert client.get_history() == []
    assert_requests_fit(provider, client.compressor)


async def test_current_trace_summary_includes_tool_names_and_arguments():
    provider = RecordingProvider()
    client = client_for(provider)
    goal = "本轮原始要求不能被历史摘要覆盖"
    current = [ChatMessage(role="user", content=goal)]
    current += tool_group("old-1", "旧文件" * 150)
    current += tool_group("old-2", "旧日志" * 150, name="bash", arguments='{"command":"pytest"}')
    latest = tool_group("latest", "最新验证结果")
    current += latest
    await client.chat(current, save_history=False)
    assert current[0].content == goal
    assert any((message.content or "").startswith(SUMMARY_PREFIX) for message in current)
    assert current[-2:] == latest
    serialized = "\n".join(messages[0].content for messages, _ in provider.summary_requests)
    assert "read_file" in serialized
    assert "src/a.py" in serialized
    assert "pytest" in serialized
    assert client.get_history() == []
    assert_requests_fit(provider, client.compressor)


async def test_uncompressible_tool_arguments_do_not_destroy_successful_history_summary():
    provider = RecordingProvider()
    client = client_for(provider, window=2048)
    original = history_pairs(4, size=150)
    client.set_history(original)
    current = [
        ChatMessage(role="user", content="继续"),
        *tool_group(arguments=json.dumps({"huge": "参" * 4000})),
    ]
    snapshot = [message.model_copy(deep=True) for message in current]
    with pytest.raises(ContextBudgetExceededError):
        await client.chat(current, save_history=False)
    assert provider.summary_requests
    assert client.get_history() == original
    assert current == snapshot
    assert provider.requests == []


async def test_summary_input_is_chunked_before_model_call():
    provider = RecordingProvider()
    compressor = ContextCompressor(
        provider,
        context_window=2048,
        max_output_tokens=64,
        safety_margin=32,
        trigger_ratio=1,
    )
    original = history_pairs(3, size=200)
    compacted = await compressor.compress(original, keep_recent=0, summary_tokens=128)
    assert len(provider.summary_requests) > 1
    assert len(compacted) == 1
    assert_requests_fit(provider, compressor)


async def test_excessive_history_does_not_spawn_unbounded_summary_calls():
    provider = RecordingProvider()
    client = client_for(provider)
    original = history_pairs(4, size=10000)
    client.set_history(original)
    with pytest.raises(ContextBudgetExceededError, match="调用上限"):
        await client.chat([ChatMessage(role="user", content="继续")], save_history=False)
    assert len(provider.summary_requests) <= client.compressor.MAX_SUMMARY_CALLS
    assert provider.requests == []
    assert client.get_history() == original


async def test_stream_uses_same_budget_gate():
    provider = RecordingProvider()
    client = client_for(provider)
    current = [ChatMessage(role="user", content="本轮要求"), *tool_group(content="日志" * 3000)]
    chunks = [chunk async for chunk in client.chat_stream(current, save_history=False)]
    assert chunks[0].delta_content == "最终回复"
    assert_requests_fit(provider, client.compressor)
    with pytest.raises(ContextBudgetExceededError):
        _ = [
            chunk
            async for chunk in client.chat_stream([ChatMessage(role="user", content="要求" * 2000)])
        ]
    assert len(provider.stream_requests) == 1


@pytest.mark.parametrize("bad_result", ["missing", "wrong", "duplicate", "orphan"])
async def test_invalid_tool_pairs_are_rejected_before_sending(bad_result):
    provider = RecordingProvider()
    client = client_for(provider)
    group = tool_group()
    if bad_result == "missing":
        current = group[:1]
    elif bad_result == "wrong":
        group[-1].tool_call_id = "wrong"
        current = group
    elif bad_result == "duplicate":
        current = group + [group[-1]]
    else:
        current = group[1:]
    with pytest.raises(ContextProtocolError):
        await client.chat(current, save_history=False)
    assert provider.requests == provider.summary_requests == []


def test_history_cut_does_not_split_question_and_answer():
    compressor = ContextCompressor(RecordingProvider(), keep_recent=3)
    history = history_pairs(4, size=2)
    assert compressor._find_cut_point(history) == 4
    with_summary = [ChatMessage(role="user", content=SUMMARY_PREFIX + "\n摘要"), *history]
    assert compressor._find_cut_point(with_summary) == 5
    assert compressor._find_cut_point([]) == 0


def test_multiple_tool_results_are_a_single_boundary():
    message = tool_group()[0]
    message.tool_calls.append(ToolCall(id="call-2", function=FunctionCall(name="bash")))
    group = [
        message,
        tool_group()[1],
        ChatMessage(role="tool", content="第二个结果", tool_call_id="call-2"),
    ]
    assert protocol_boundaries(group) == [0, 3]
    compressor = ContextCompressor(RecordingProvider(), keep_recent=1)
    assert compressor._find_cut_point(group, whole_turns=False) == 0


async def test_tool_group_split_across_history_and_current_remains_valid():
    provider = RecordingProvider()
    client = client_for(provider)
    group = tool_group(content="最新输出" * 1000)
    client.set_history(
        history_pairs(4, size=80) + [ChatMessage(role="user", content="尚未结束的任务"), group[0]]
    )
    current = [group[-1]]
    await client.chat(current, save_history=False)
    assert client.get_history()[-1].tool_calls == group[0].tool_calls
    protocol_boundaries(client.get_history() + current)
    assert_requests_fit(provider, client.compressor)


async def test_cancelled_summary_rolls_back_state():
    provider = RecordingProvider()
    started = asyncio.Event()

    async def blocking(*args, **kwargs):
        started.set()
        await asyncio.Event().wait()

    provider.chat = blocking
    client = client_for(provider)
    original = history_pairs(4, size=120)
    client.set_history(original)
    client.compressor.summary = "before"
    current = [ChatMessage(role="user", content="继续")]
    task = asyncio.create_task(client._prepare_messages(current, None))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert client.get_history() == original
    assert client.compressor.summary == "before"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"keep_recent": 0},
        {"keep_recent": -1},
        {"trigger_ratio": 0},
        {"trigger_ratio": 1.1},
        {"trigger_ratio": float("nan")},
        {"safety_margin": -1},
        {"context_window": 64, "max_output_tokens": 64, "safety_margin": 0},
    ],
)
def test_invalid_compression_configuration_fails_early(kwargs):
    with pytest.raises(ValueError):
        ContextCompressor(RecordingProvider(), **kwargs)


def test_output_reservation_cannot_be_smaller_than_actual_generation_limit():
    client = AIClient(RecordingProvider(), max_tokens=64)
    with pytest.raises(ValueError, match="输出预留"):
        client.enable_compression(max_output_tokens=32)


async def test_changing_generation_limit_requires_reconfiguring_compression():
    provider = RecordingProvider()
    client = client_for(provider)
    client.max_tokens = 1000
    with pytest.raises(ValueError, match="输出预留"):
        await client.chat([ChatMessage(role="user", content="你好")])
    assert provider.requests == []


def test_mixed_text_chunks_respect_estimated_limits():
    text = "中文abc😀\n" * 100
    pieces = split_text(text, 17)
    assert "".join(pieces) == text
    assert all(estimate_tokens(piece) <= 17 for piece in pieces)


async def test_summary_timeout_is_bounded_and_does_not_erase_history():
    provider = RecordingProvider()
    cancelled = asyncio.Event()

    async def hanging(*args, **kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    provider.chat = hanging
    client = client_for(provider)
    client.compressor.SUMMARY_TIMEOUT = 0.01
    original = history_pairs(4, size=120)
    client.set_history(original)
    with pytest.raises(ContextBudgetExceededError, match="超时"):
        await client._prepare_messages([ChatMessage(role="user", content="继续")], None)
    assert cancelled.is_set()
    assert client.get_history() == original


async def test_agent_persists_only_user_and_final_reply_after_tool_truncation(tmp_path):
    from xclaw.coding import CodingAgent
    from xclaw.core.execution import Capability
    from xclaw.core.tool import Tool

    class ToolProvider(RecordingProvider):
        async def chat(self, messages, tools=None, **kwargs):
            response = await super().chat(messages, tools, **kwargs)
            if len(messages) == 1 and (messages[0].content or "").startswith(
                ContextCompressor.SUMMARY_PROMPT
            ):
                return response
            if len(self.requests) == 1:
                return ChatResponse(
                    tool_calls=[
                        ToolCall(
                            id="read-1",
                            function=FunctionCall(name="read_code", arguments="{}"),
                        )
                    ]
                )
            return response

    async def read_code() -> str:
        return "开头\n" + "代码片段" * 2000 + "\n结尾"

    provider = ToolProvider()
    agent = CodingAgent(
        provider,
        max_tokens=64,
        enable_context_compression=True,
        context_window=1024,
        compression_trigger_ratio=1,
        compression_safety_margin=32,
        workspace_dir=str(tmp_path),
        register_default_tools=False,
    )
    agent.register_tool(
        Tool(
            "read_code",
            "读取代码",
            read_code,
            capabilities=frozenset({Capability.FILE_READ}),
        )
    )
    goal = "读取代码后给出最终结论，禁止修改文件"
    assert await agent.run(goal) == "最终回复"
    assert agent.client.get_history() == [
        ChatMessage(role="user", content=goal),
        ChatMessage(role="assistant", content="最终回复"),
    ]
    assert any(
        message.role == "tool" and TRUNCATION_MARKER in (message.content or "")
        for message in provider.requests[-1][0]
    )
    assert_requests_fit(provider, agent.client.compressor)
    await agent.sandbox_router.stop()
