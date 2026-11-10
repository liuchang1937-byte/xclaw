"""DSML 恢复只执行完整且经过工具 Schema 校验的模型调用。"""

import json

import pytest

from xclaw.ai.client import AIClient
from xclaw.ai.context import estimate_messages_tokens, estimate_tokens
from xclaw.ai.provider import BaseProvider
from xclaw.ai.tool_protocol import InvalidToolCallResponseError, normalize_tool_response
from xclaw.ai.types import ChatMessage, ChatResponse, ToolDefinition


class ScriptedProvider(BaseProvider):
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    async def chat(self, messages, tools=None, **kwargs):
        self.requests.append([message.model_copy(deep=True) for message in messages])
        return self.responses.pop(0)

    async def chat_stream(self, *args, **kwargs):
        raise AssertionError("应使用完整响应进行协议检查")
        yield


def test_retained_reasoning_is_included_in_request_budget():
    plain = ChatMessage(role="assistant", content="完成")
    retained = plain.model_copy(update={"reasoning_content": "opaque" * 100})
    assert estimate_messages_tokens([retained]) - estimate_messages_tokens([plain]) == (
        estimate_tokens(retained.reasoning_content)
    )


TOOLS = [
    ToolDefinition(
        name="echo",
        description="",
        parameters={
            "type": "object",
            "properties": {"text": {"type": "string"}, "count": {"type": "integer"}},
            "required": ["text"],
            "additionalProperties": False,
        },
    )
]
DSML = (
    '<|DSML|tool_calls><|DSML|invoke name="echo">'
    '<|DSML|parameter name="text" string="true">学生名单\n含 & 和 &lt;'
    '</|DSML|parameter><|DSML|parameter name="count" string="false">2'
    "</|DSML|parameter></|DSML|invoke></|DSML|tool_calls>"
)


def test_recovery_preserves_parameters_and_reasoning_without_displaying_protocol():
    response = normalize_tool_response(
        ChatResponse(content=DSML, reasoning_content="opaque"), TOOLS
    )
    assert response.content is None and response.finish_reason == "tool_calls"
    assert response.reasoning_content == "opaque"
    assert json.loads(response.tool_calls[0].function.arguments) == {
        "text": "学生名单\n含 & 和 &lt;",
        "count": 2,
    }
    assert normalize_tool_response(ChatResponse(content=DSML.replace("|", "｜")), TOOLS).tool_calls


@pytest.mark.parametrize(
    "content",
    [
        "这是示例：" + DSML,
        "```\n" + DSML + "\n```",
        DSML[:-10],
        DSML.replace('name="echo"', 'name="unknown"'),
        DSML.replace('name="count"', 'name="extra"'),
        DSML.replace('string="false">2', 'string="false">true'),
    ],
)
def test_malformed_unknown_or_example_calls_are_not_executed(content):
    with pytest.raises(InvalidToolCallResponseError):
        normalize_tool_response(ChatResponse(content=content), TOOLS)


async def test_invalid_protocol_retries_without_saving_raw_text():
    provider = ScriptedProvider(
        [
            ChatResponse(content="例子：" + DSML),
            ChatResponse(content=DSML),
        ]
    )
    client = AIClient(provider)
    response = await client.chat([ChatMessage(role="user", content="执行")], TOOLS)
    assert response.tool_calls and client.get_history()[-1].content is None
    assert "标准 tool_calls" in provider.requests[-1][-1].content


async def test_persistent_invalid_protocol_fails_clearly_and_leaves_history_clean():
    client = AIClient(ScriptedProvider([ChatResponse(content="例子：" + DSML)] * 3))
    with pytest.raises(InvalidToolCallResponseError, match="连续"):
        await client.chat([ChatMessage(role="user", content="执行")], TOOLS)
    assert client.get_history() == []


async def test_deepseek_stream_never_displays_dsml_or_reasoning():
    client = AIClient(
        ScriptedProvider([ChatResponse(content=DSML, reasoning_content="opaque")]),
        model="deepseek-v4-pro",
    )
    chunks = [
        chunk
        async for chunk in client.chat_stream(
            [ChatMessage(role="user", content="执行")],
            TOOLS,
        )
    ]
    assert all(not chunk.delta_content for chunk in chunks)
    assert chunks[0].delta_tool_calls and chunks[0].delta_reasoning_content == "opaque"
