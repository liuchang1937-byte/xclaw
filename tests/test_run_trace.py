import asyncio
import json

import pytest

from xclaw.ai.context import ContextCompressor
from xclaw.ai.provider import BaseProvider
from xclaw.ai.types import ChatMessage, ChatResponse, StreamChunk, ToolCall
from xclaw.coding import CodingAgent
from xclaw.core.execution import Capability
from xclaw.core.scheduling import PLANNING_PROMPT
from xclaw.core.tool import Tool
from xclaw.telemetry import RunTrace, activate, observed_chat


class Provider(BaseProvider):
    def __init__(self, responses):
        self.responses = iter(responses)

    async def chat(self, messages, tools=None, **kwargs):
        if messages and messages[0].content == PLANNING_PROMPT:
            return ChatResponse(
                content=json.dumps(
                    {
                        "mode": "parallel",
                        "steps": [
                            {"id": "a", "depends_on": []},
                            {"id": "b", "depends_on": []},
                        ],
                    }
                ),
                usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            )
        result = next(self.responses)
        if isinstance(result, Exception):
            raise result
        return result

    async def chat_stream(self, *args, **kwargs):
        yield StreamChunk(delta_content="done")
        yield StreamChunk(usage={"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5})


async def test_trace_counts_auxiliary_calls_and_safe_parallel_tools(tmp_path):
    calls = [ToolCall(id=i, function={"name": "read", "arguments": "{}"}) for i in ("a", "b")]
    provider = Provider([ChatResponse(tool_calls=calls), ChatResponse(content="done")])
    bot = CodingAgent(
        provider,
        workspace_dir=str(tmp_path),
        register_default_tools=False,
        trace_dir=str(tmp_path / "traces"),
    )

    async def read():
        return "private result not recorded"

    bot.register_tool(
        Tool(
            "read", "read", read, capabilities=frozenset({Capability.FILE_READ}), parallel_safe=True
        )
    )
    assert await bot.run("private input not recorded") == "done"
    metrics = bot.last_trace.metrics()
    assert metrics["model_calls"] == 3
    assert metrics["reported_tokens"] == 15
    assert metrics["usage_complete"] is False
    assert metrics["calls_by_purpose"] == {"main": 2, "scheduling": 1}
    assert metrics["tool_calls"] == 2
    text = bot.last_trace.path.read_text()
    assert "private input" not in text and "private result" not in text
    events = [json.loads(line) for line in text.splitlines()]
    assert len({e["run_id"] for e in events}) == 1
    assert events[-1]["status"] == "completed"
    await bot.sandbox_router.stop()


async def test_stream_usage_and_cancelled_run_are_recorded(tmp_path):
    bot = CodingAgent(
        Provider([]), model="fake", workspace_dir=str(tmp_path), register_default_tools=False
    )
    assert await bot.run("hello", stream_callback=lambda _: asyncio.sleep(0)) == "done"
    assert bot.last_trace.metrics()["reported_tokens"] == 5

    started = asyncio.Event()

    async def block(*args, **kwargs):
        started.set()
        await asyncio.Event().wait()

    bot.client.provider.chat = block
    job = asyncio.create_task(bot.run("cancel"))
    await started.wait()
    with pytest.raises(RuntimeError, match="并发"):
        await bot.run("reenter")
    job.cancel()
    with pytest.raises(asyncio.CancelledError):
        await job
    assert bot.last_run_status == "cancelled"
    assert bot._is_running is False
    assert bot.last_trace.events[-1]["status"] == "cancelled"
    assert bot.last_trace.metrics()["model_errors"] == 1
    await bot.sandbox_router.stop()


async def test_summary_usage_and_failed_provider_attempts_are_not_lost():
    usage = {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10}
    trace = RunTrace()
    provider = Provider([ChatResponse(content="short", usage=usage), ValueError("private")])
    compressor = ContextCompressor(provider, keep_recent=2)
    history = [
        ChatMessage(role="user", content="old" * 100),
        ChatMessage(role="assistant", content="old" * 100),
        ChatMessage(role="user", content="new"),
        ChatMessage(role="assistant", content="new"),
    ]
    with activate(trace):
        assert len(await compressor.compress(history)) == 3
        with pytest.raises(ValueError):
            await observed_chat(provider, [], model="fake")
    metrics = trace.metrics()
    assert metrics["model_calls"] == 2 and metrics["reported_tokens"] == 10
    assert metrics["model_errors"] == 1
    assert metrics["calls_by_purpose"] == {"compression": 1, "main": 1}


async def test_stream_callback_failure_closes_model_span_before_run_finishes(tmp_path):
    bot = CodingAgent(
        Provider([]), model="fake", workspace_dir=str(tmp_path), register_default_tools=False
    )

    async def fail(_):
        raise ValueError("consumer failed")

    with pytest.raises(ValueError):
        await bot.run("hello", stream_callback=fail)
    assert bot.last_trace.metrics()["model_calls"] == 1
    assert bot.last_trace.metrics()["usage_complete"] is False
    assert bot.last_trace.events[-1]["event"] == "run.finished"
    assert bot.last_run_status == "error"
    await bot.sandbox_router.stop()


@pytest.mark.parametrize("strategy", ["summary", "truncate"])
async def test_context_baselines_share_budget_and_preserve_current_user_input(tmp_path, strategy):
    class ContextProvider(Provider):
        async def chat(self, messages, tools=None, **kwargs):
            if messages[0].content.startswith(ContextCompressor.SUMMARY_PROMPT):
                return ChatResponse(content="RULE_X")
            self.sent = messages
            return ChatResponse(content="done")

    provider = ContextProvider([])
    bot = CodingAgent(
        provider,
        workspace_dir=str(tmp_path),
        register_default_tools=False,
        enable_context_compression=True,
        context_strategy=strategy,
        context_window=4000,
        max_tokens=128,
        compression_safety_margin=200,
    )
    bot.client.set_history(
        [
            ChatMessage(role="user", content="RULE_X " + "old" * 6000),
            ChatMessage(role="assistant", content="acknowledged"),
            ChatMessage(role="user", content="recent"),
            ChatMessage(role="assistant", content="noted"),
        ]
    )
    assert await bot.run("CURRENT_USER_INPUT") == "done"
    assert provider.sent[-1].content == "CURRENT_USER_INPUT"
    assert any("RULE_X" in (m.content or "") for m in provider.sent) is (strategy == "summary")
    metrics = bot.last_trace.metrics()
    assert (metrics["calls_by_purpose"].get("compression", 0) > 0) is (strategy == "summary")
    event = next(e for e in bot.last_trace.events if e["event"] == "context.prepared")
    assert event["after_estimated_tokens"] <= event["budget"]
    await bot.sandbox_router.stop()
