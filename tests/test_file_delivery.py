"""飞书请求必须得到实际附件；失败、越界和并发请求不得误报交付成功。"""

import asyncio
import json
import time

import httpx
import pytest

from xclaw.ai.provider import BaseProvider
from xclaw.ai.types import ChatResponse, FunctionCall, ToolCall
from xclaw.coding import CodingAgent
from xclaw.coding.excel_tool import register_excel_tool
from xclaw.im.adapter import IMMessage, IMReply
from xclaw.im.delivery import collect_deliveries, register_delivery_tool, validate_file
from xclaw.im.feishu import FeishuAdapter
from xclaw.sandbox import ExecutionPolicy
from xclaw.server.config import Config
from xclaw.server.main import XClawServer

openpyxl = pytest.importorskip("openpyxl")


def workbook(path):
    book = openpyxl.Workbook()
    book.active.title = "九一班"
    book.active.append(["学号", "姓名", "班级"])
    book.active.append(["090101", "张明", "九一班"])
    book.save(path)
    book.close()


def call(name, **arguments):
    return ChatResponse(
        tool_calls=[
            ToolCall(
                id=f"call_{name}",
                function=FunctionCall(name=name, arguments=json.dumps(arguments)),
            )
        ],
        reasoning_content="opaque-provider-state",
    )


class ScriptedProvider(BaseProvider):
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    async def chat(self, messages, tools=None, **kwargs):
        self.requests.append([message.model_copy(deep=True) for message in messages])
        return self.responses.pop(0)

    async def chat_stream(self, *args, **kwargs):
        raise AssertionError("IM 应仅交付最终结果")
        yield


async def test_feishu_request_recovers_dsml_creates_and_sends_actual_excel(tmp_path):
    output = tmp_path / "九一班.xlsx"
    dsml = (
        '<｜DSML｜tool_calls><｜DSML｜invoke name="create_excel">'
        '<｜DSML｜parameter name="path" string="true">九一班.xlsx'
        '</｜DSML｜parameter><｜DSML｜parameter name="columns" string="false">'
        '["学号","姓名","班级"]</｜DSML｜parameter>'
        '<｜DSML｜parameter name="rows" string="false">'
        '[["090101","张明","九一班"]]</｜DSML｜parameter>'
        '<｜DSML｜parameter name="sheet_name" string="true">九一班'
        "</｜DSML｜parameter></｜DSML｜invoke></｜DSML｜tool_calls>"
    )
    provider = ScriptedProvider(
        [
            ChatResponse(content=dsml, reasoning_content="opaque-provider-state"),
            # 没有交付附件的文本回复必须被纠正，不能冒充最终文件。
            ChatResponse(content="已经创建，请查看九一班.xlsx。"),
            call("deliver_file", path="九一班.xlsx"),
            ChatResponse(content="九一班学生信息表已完成。"),
        ]
    )
    agent = CodingAgent(provider, workspace_dir=str(tmp_path), register_default_tools=False)

    register_excel_tool(agent.tool_registry)
    register_delivery_tool(agent.tool_registry)
    requests = []

    def handle(request):
        requests.append(request)
        if request.url.path.endswith("/files"):
            assert b'filename="' in request.content
            assert b"PK\x03\x04" in request.content
            assert b"xls" in request.content
            return httpx.Response(200, json={"code": 0, "data": {"file_key": "file_demo"}})
        return httpx.Response(200, json={"code": 0, "data": {"message_id": "reply"}})

    adapter = FeishuAdapter(app_id="test", app_secret="test")
    adapter._http_client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    adapter._tenant_access_token = "test-token"
    adapter._token_expires_at = time.time() + 1000
    server = object.__new__(XClawServer)
    server.config = Config(agent={
        "user_workspace_dir": tmp_path / "users", "workspace_dir": str(tmp_path / "deployment"),
    })
    server.im_adapter = adapter
    server._session_locks = {}
    server._sessions_dir = tmp_path / "sessions"
    server._get_or_create_agent = lambda *_: agent
    adapter.on_message(server._handle_im_message)
    message = IMMessage(
        platform="feishu",
        message_id="om_request",
        chat_id="oc_private",
        chat_type="p2p",
        sender_id="ou_user",
        content="帮我创建一个excel表格，九一班学生信息，写完发给我",
    )
    try:
        await adapter._dispatch(message)
        assert [request.url.path for request in requests] == [
            "/open-apis/im/v1/files",
            "/open-apis/im/v1/messages/om_request/reply",
            "/open-apis/im/v1/messages",
        ]
        sent_file = json.loads(requests[1].content)
        assert sent_file["msg_type"] == "file"
        assert json.loads(sent_file["content"]) == {"file_key": "file_demo"}
        sent_text = json.loads(json.loads(requests[2].content)["content"])["text"]
        assert "DSML" not in sent_text and "opaque" not in sent_text
        assert sent_text == "九一班学生信息表已完成。"
        book = openpyxl.load_workbook(output)
        assert book["九一班"]["B2"].value == "张明"
        book.close()
        assert any("deliver_file" in (m.content or "") for m in provider.requests[2])
        assistant = next(m for m in provider.requests[1] if m.role == "assistant")
        assert assistant.to_openai_format()["reasoning_content"] == "opaque-provider-state"
    finally:
        await adapter.stop()
        await agent.sandbox_router.stop()


async def test_deliveries_are_per_request_and_workspace_policy_applies(tmp_path):
    agent = CodingAgent(
        ScriptedProvider([]), workspace_dir=str(tmp_path), register_default_tools=False
    )
    register_delivery_tool(agent.tool_registry)
    for name in ("one.xlsx", "two.xlsx"):
        workbook(tmp_path / name)
    (tmp_path / ".env").write_text("private")

    async def enqueue(name):
        with collect_deliveries() as files:
            await asyncio.sleep(0)
            result = await agent.tool_registry.execute("deliver_file", json.dumps({"path": name}))
            assert "最终附件" in result
            await asyncio.sleep(0)
            return [path.name for path in files]

    try:
        assert await asyncio.gather(enqueue("one.xlsx"), enqueue("two.xlsx")) == [
            ["one.xlsx"],
            ["two.xlsx"],
        ]
        with collect_deliveries() as files:
            assert "拒绝" in await agent.tool_registry.execute("deliver_file", '{"path":".env"}')
            assert "工作区" in await agent.tool_registry.execute(
                "deliver_file",
                '{"path":"../outside.xlsx"}',
            )
            assert files == []
        assert "当前 IM" in await agent.tool_registry.execute(
            "deliver_file",
            '{"path":"one.xlsx"}',
        )
    finally:
        await agent.sandbox_router.stop()


@pytest.mark.parametrize("failure", ["upload", "message"])
async def test_upload_or_message_failure_never_claims_file_was_sent(tmp_path, failure):
    output = tmp_path / "final.xlsx"
    workbook(output)
    texts = []

    def handle(request):
        if request.url.path.endswith("/files"):
            result = (
                {"code": 99991672}
                if failure == "upload"
                else {
                    "code": 0,
                    "data": {"file_key": "file_test"},
                }
            )
            return httpx.Response(200, json=result)
        if json.loads(request.content)["msg_type"] == "file":
            return httpx.Response(200, json={"code": 99991672})
        texts.append(json.loads(json.loads(request.content)["content"])["text"])
        return httpx.Response(200, json={"code": 0})

    adapter = FeishuAdapter()
    adapter._tenant_access_token = "test"
    adapter._token_expires_at = time.time() + 1000
    adapter._http_client = httpx.AsyncClient(transport=httpx.MockTransport(handle))

    async def reply(_):
        return IMReply("已成功发送！", [output])

    adapter.on_message(reply)
    try:
        await adapter._dispatch(IMMessage("feishu", "om_in", "oc_chat", "group", "ou_user"))
        assert len(texts) == 1 and "发送失败" in texts[0]
        assert "成功" not in texts[0]
    finally:
        await adapter.stop()


def test_invalid_xlsx_is_not_queued(tmp_path):
    path = tmp_path / "fake.xlsx"
    path.write_text("这不是 Excel 文件")
    with pytest.raises(ValueError, match="有效"):
        validate_file(path)


async def test_native_excel_preserves_text_and_denies_overwrite_or_view_writes(tmp_path):
    agent = CodingAgent(
        ScriptedProvider([]), workspace_dir=str(tmp_path), register_default_tools=False
    )
    register_excel_tool(agent.tool_registry)
    arguments = json.dumps(
        {
            "path": "final.xlsx",
            "columns": ["编号", "原始文本"],
            "rows": [["0001", "=1+1"]],
            "sheet_name": "学生",
        }
    )
    try:
        assert "已创建并验证" in await agent.tool_registry.execute("create_excel", arguments)
        original = (tmp_path / "final.xlsx").read_bytes()
        assert "失败" in await agent.tool_registry.execute("create_excel", arguments)
        assert (tmp_path / "final.xlsx").read_bytes() == original
        book = openpyxl.load_workbook(tmp_path / "final.xlsx")
        assert book["学生"]["A2"].value == "0001"
        assert book["学生"]["B2"].value == "=1+1"
        assert book["学生"]["B2"].data_type == "s"
        book.close()
        agent.sandbox_router.policy = ExecutionPolicy(mode="view")
        assert "create_excel" not in agent.tool_registry.list_tools()
        assert "拒绝" in await agent.tool_registry.execute(
            "create_excel",
            arguments.replace("final.xlsx", "other.xlsx"),
        )
        assert not (tmp_path / "other.xlsx").exists()
    finally:
        await agent.sandbox_router.stop()
