import json

import httpx
import pytest

from examples.agentbench_fc import Controller, messages_from_environment, run_sample, summarize
from xclaw.ai.client import AIClient
from xclaw.ai.types import ChatMessage, ChatResponse, ToolCall

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "bash_action",
            "description": "Execute bash in the benchmark container",
            "parameters": {
                "type": "object",
                "properties": {"script": {"type": "string"}},
                "required": ["script"],
            },
        },
    }
]


class Provider:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.requests = []

    async def chat(self, messages, tools=None, **kwargs):
        self.requests.append((messages, tools))
        value = next(self.responses)
        if isinstance(value, Exception):
            raise value
        return value


def action():
    return ChatResponse(
        tool_calls=[
            ToolCall(
                id="call-1",
                function={
                    "name": "bash_action",
                    "arguments": '{"script":"pwd"}',
                },
            )
        ],
        finish_reason="tool_calls",
        usage={
            "prompt_tokens": 30,
            "completion_tokens": 10,
            "total_tokens": 40,
        },
    )


@pytest.mark.parametrize("reward,expected", [(1.0, True), (0.0, False), (0.5, False), (None, None)])
async def test_official_protocol_and_reward_not_completed_status(tmp_path, reward, expected):
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path == "/api/get_indices":
            assert request.url.params["name"] == "os-std"
            return httpx.Response(200, json=["std-001-0"])
        if request.url.path == "/api/start_sample":
            assert json.loads(request.content) == {
                "name": "os-std",
                "index": "std-001-0",
                "custom_task": None,
            }
            return httpx.Response(
                200,
                headers={"session_id": "42"},
                json={
                    "messages": [
                        {"role": "system", "content": "Use the official tools"},
                        {"role": "user", "content": "Show current directory"},
                    ],
                    "tools": TOOLS,
                },
            )
        assert request.url.path == "/api/interact"
        assert request.headers["session_id"] == "42"
        message = json.loads(request.content)["messages"][0]
        assert message["tool_calls"][0]["function"]["name"] == "bash_action"
        return httpx.Response(
            200,
            json={
                "finish": True,
                "status": "completed",
                "reward": reward,
                "metrics": {"score": 0.7},
            },
        )

    controller = Controller("http://fixture/api", transport=httpx.MockTransport(handler))
    try:
        assert await controller.indices("os-std") == ["std-001-0"]
        provider = Provider([action()])
        row = await run_sample(
            controller,
            AIClient(provider, model="fixture"),
            "os-std",
            "std-001-0",
            tmp_path / "sample",
        )
        assert row["success"] is expected
        assert row["official_result"]["metrics"]["score"] == 0.7
        assert row["metrics"]["reported_tokens"] == 40
        assert provider.requests[0][1][0].name == "bash_action"
        assert [m.role for m in provider.requests[0][0]] == ["system", "user"]
    finally:
        await controller.client.aclose()


@pytest.mark.parametrize("stop", ["length", "empty", "limit", "timeout"])
async def test_incomplete_sessions_cancel_and_never_become_success(tmp_path, stop):
    paths = []

    def handler(request):
        paths.append(request.url.path)
        if request.url.path.endswith("cancel"):
            return httpx.Response(200, json={})
        if request.url.path.endswith("interact"):
            return httpx.Response(
                200,
                json={"messages": [{"role": "tool", "tool_call_id": "call-1", "content": "/tmp"}]},
            )
        return httpx.Response(
            200,
            headers={"session_id": "42"},
            json={
                "messages": [{"role": "user", "content": "Task"}],
                "tools": TOOLS,
            },
        )

    class SlowProvider(Provider):
        async def chat(self, *args, **kwargs):
            import asyncio

            await asyncio.sleep(1)

    response = ChatResponse(finish_reason="length") if stop == "length" else ChatResponse()
    provider = (
        SlowProvider([])
        if stop == "timeout"
        else Provider([action() if stop == "limit" else response])
    )
    controller = Controller("http://fixture/api", transport=httpx.MockTransport(handler))
    try:
        row = await run_sample(
            controller,
            AIClient(provider),
            "os-std",
            0,
            tmp_path / "sample",
            max_turns=1,
            timeout=0.01 if stop == "timeout" else 5,
        )
        assert (
            row["status"]
            == {
                "length": "model_output_truncated",
                "empty": "empty_model_response",
                "limit": "client_turn_limit",
                "timeout": "timeout",
            }[stop]
        )
        assert row["success"] is None
        assert row["cancelled"]
        assert paths[-1] == "/api/cancel"
    finally:
        await controller.client.aclose()


def test_history_deltas_full_reset_and_text_only_boundary():
    history = [ChatMessage(role="user", content="old")]
    delta = [{"role": "tool", "tool_call_id": "a", "content": "output"}]
    assert len(messages_from_environment(history, delta)) == 2
    reset = messages_from_environment(
        history,
        [
            {"role": "system", "content": "new"},
            {"role": "user", "content": [{"type": "text", "text": "task"}]},
        ],
    )
    assert [m.content for m in reset] == ["new", "task"]
    with pytest.raises(ValueError, match="text environments"):
        messages_from_environment([], [{"role": "user", "content": [{"type": "image_url"}]}])


def test_summary_exposes_missing_judgments_and_error_denominator():
    result = summarize([{"success": True}, {"success": False}, {"success": None}])
    assert result["success_rate_all_attempts"] == 1 / 3
    assert result["judged_success_rate"] == 0.5
    assert result["unknown_or_errors"] == 1
    assert not result["complete_official_judgments"]
    assert summarize([])["judged_success_rate"] is None


async def test_empty_environment_observation_is_not_a_model_failure(tmp_path):
    def handler(request):
        if request.url.path.endswith("cancel"):
            return httpx.Response(200, json={})
        return httpx.Response(
            200, headers={"session_id": "42"}, json={"messages": [], "tools": TOOLS}
        )

    controller = Controller("http://fixture/api", transport=httpx.MockTransport(handler))
    provider = Provider([])
    try:
        row = await run_sample(controller, AIClient(provider), "os-std", 0, tmp_path / "sample")
        assert row["status"] == "environment_empty_observation"
        assert row["success"] is None
        assert row["metrics"]["model_calls"] == 0
        assert row["cancelled"]
    finally:
        await controller.client.aclose()
