"""流式技能工具调用必须保留多个调用的索引、ID 和累计参数。"""

import json
from types import SimpleNamespace

import pytest

from xclaw.ai.client import AIClient
from xclaw.ai.provider import BaseProvider, OpenAIProvider
from xclaw.ai.streaming import StreamAccumulator
from xclaw.ai.types import ChatMessage, ChatResponse, FunctionCall, StreamChunk, ToolCall


def snapshot(index, call_id, name, arguments):
    return ToolCall(index=index, id=call_id, function=FunctionCall(name=name, arguments=arguments))


class StreamProvider(BaseProvider):
    async def chat(self, *args, **kwargs):
        return ChatResponse(content="已加载技能")

    async def chat_stream(self, *args, **kwargs):
        yield StreamChunk(delta_content="加载技能")
        yield StreamChunk(delta_tool_calls=[snapshot(0, "word-call", "word", '{"user_input":')])
        yield StreamChunk(
            delta_tool_calls=[snapshot(1, "excel-call", "excel", '{"user_input":"Excel"}')]
        )
        yield StreamChunk(
            delta_tool_calls=[snapshot(0, "word-call", "word", '{"user_input":"Word"}')],
            finish_reason="tool_calls",
        )


async def test_ai_client_stream_saves_both_tool_calls_without_transport_index():
    client = AIClient(StreamProvider())
    _ = [
        chunk async for chunk in client.chat_stream([ChatMessage(role="user", content="加载技能")])
    ]
    message = client.get_history()[-1]
    assert [call.function.name for call in message.tool_calls] == ["word", "excel"]
    assert message.tool_calls[0].function.arguments == '{"user_input":"Word"}'
    assert all("index" not in call.model_dump() for call in message.tool_calls)


async def test_agent_stream_can_activate_multiple_skills(tmp_path):
    from xclaw.coding import CodingAgent

    agent = CodingAgent(StreamProvider(), workspace_dir=str(tmp_path))
    parts = []

    async def callback(text):
        parts.append(text)

    assert await agent.run("加载 Word 和 Excel", stream_callback=callback) == "已加载技能"
    assert parts == ["加载技能"]
    assert [message.role for message in agent.client.get_history()] == ["user", "assistant"]
    await agent.sandbox_router.stop()


def test_accumulator_supports_legacy_calls_and_rejects_incomplete_or_duplicate_ids():
    accumulator = StreamAccumulator()
    call = ToolCall(id="legacy", function=FunctionCall(name="word", arguments="{}"))
    accumulator.add(StreamChunk(delta_tool_calls=[call]))
    assert accumulator.response().tool_calls[0].id == "legacy"
    accumulator.add(StreamChunk(delta_tool_calls=[snapshot(1, "legacy", "excel", "{}")]))
    with pytest.raises(ValueError, match="重复"):
        accumulator.response()
    broken = StreamAccumulator()
    broken.add(StreamChunk(delta_tool_calls=[snapshot(0, "", "", "{")]))
    with pytest.raises(ValueError, match="不完整"):
        broken.response()


async def test_openai_provider_exposes_indices_for_interleaved_native_deltas():
    chunks = []
    for index, call_id, name, arguments in (
        (0, "first", "word", '{"user_input":'),
        (1, "second", "excel", '{"user_input":"Excel"}'),
        (0, None, None, '"Word"}'),
    ):
        delta = SimpleNamespace(
            content=None,
            tool_calls=[
                SimpleNamespace(
                    index=index,
                    id=call_id,
                    function=SimpleNamespace(name=name, arguments=arguments),
                )
            ],
        )
        chunks.append(SimpleNamespace(choices=[SimpleNamespace(delta=delta, finish_reason=None)]))

    async def sdk_stream():
        for chunk in chunks:
            yield chunk

    async def create(**kwargs):
        return sdk_stream()

    provider = object.__new__(OpenAIProvider)
    provider._client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )
    accumulator = StreamAccumulator()
    observed = []
    async for chunk in provider.chat_stream(
        [ChatMessage(role="user", content="test")], model="test"
    ):
        observed.extend(call.index for call in chunk.delta_tool_calls)
        accumulator.add(chunk)
    assert observed == [0, 1, 0]
    assert [
        json.loads(call.function.arguments)["user_input"]
        for call in accumulator.response().tool_calls
    ] == ["Word", "Excel"]
