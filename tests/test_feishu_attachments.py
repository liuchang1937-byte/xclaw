"""真实 HTTP 错误状态中的权限诊断，以及飞书上传附件的下载/读取链路。"""

import asyncio
import hashlib
import io
import json
import time
from types import SimpleNamespace

import httpx
from loguru import logger

from tests.test_file_delivery import ScriptedProvider, call, workbook
from xclaw.ai.types import ChatResponse
from xclaw.coding import CodingAgent
from xclaw.coding.excel_tool import register_excel_tool
from xclaw.im.adapter import IMMessage
from xclaw.im.feishu import FeishuAdapter
from xclaw.server.config import Config
from xclaw.server.main import XClawServer


def adapter_for(handler):
    adapter = FeishuAdapter(app_secret="test-secret")
    adapter._http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter._tenant_access_token = "test-token"
    adapter._token_expires_at = time.time() + 1000
    return adapter


async def test_http_400_keeps_real_permission_error_code_and_redacts_credentials(tmp_path):
    path = tmp_path / "students.xlsx"
    workbook(path)
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(
            400,
            json={
                "code": 99991672,
                "msg": "Access denied. Required im:resource. test-token test-secret",
                "error": {"log_id": "test-log-id"},
            },
        )

    adapter = adapter_for(handle)
    log = io.StringIO()
    sink = logger.add(log)
    try:
        assert not await adapter.send_file("oc_chat", path)
        assert "99991672" in adapter.file_failure_reason()
        assert "im:resource" in adapter.file_failure_reason()
        assert "HTTP=400" in log.getvalue() and "test-log-id" in log.getvalue()
        assert "test-token" not in log.getvalue() and "test-secret" not in log.getvalue()
        assert len(requests) == 1
    finally:
        logger.remove(sink)
        await adapter.stop()


async def test_websocket_and_http_events_preserve_filename_and_resource_key(monkeypatch):
    captured = []
    adapter = FeishuAdapter()
    monkeypatch.setattr(adapter, "_dispatch_safe", captured.append)

    async def dispatch(message):
        captured.append(message)

    monkeypatch.setattr(adapter, "_dispatch", dispatch)
    data = {
        "message_id": "om_original",
        "chat_id": "oc_chat",
        "chat_type": "p2p",
        "message_type": "file",
        "content": json.dumps({"file_key": "file_test", "file_name": "九一班.xlsx"}),
    }
    inner = {"message": data, "sender": {"sender_id": {"open_id": "ou_user"}}}
    adapter._on_fs_message_event(SimpleNamespace(event=inner))
    adapter.handle_event_callback(
        {"header": {"event_type": "im.message.receive_v1"}, "event": inner}
    )
    await asyncio.sleep(0)
    assert len(captured) == 2
    for message in captured:
        assert message.attachments == [
            {"type": "file", "file_key": "file_test", "name": "九一班.xlsx"}
        ]
        assert "九一班.xlsx" in message.content


async def test_received_excel_downloads_from_original_message_and_is_read_by_agent(tmp_path):
    original = tmp_path / "source.xlsx"
    workbook(original)
    incoming_bytes = original.read_bytes()
    requests = []

    def handle(request):
        requests.append(request)
        if request.method == "GET":
            assert request.url.path.endswith("/messages/om_original/resources/file_test")
            assert request.url.params["type"] == "file"
            return httpx.Response(
                200, content=incoming_bytes, headers={"Content-Type": "application/octet-stream"}
            )
        return httpx.Response(200, json={"code": 0})

    adapter = adapter_for(handle)
    identifier = hashlib.sha256(b"om_original\0file_test").hexdigest()[:20]
    relative = f"inputs/feishu/{identifier}/九一班.xlsx"
    provider = ScriptedProvider(
        [
            call("inspect_excel", path=relative),
            ChatResponse(content="已读取九一班工作表：表头为学号、姓名、班级，共 1 名学生。"),
        ]
    )
    agent = CodingAgent(provider, workspace_dir=str(tmp_path), register_default_tools=False)
    register_excel_tool(agent.tool_registry)
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
        "feishu",
        "om_original",
        "oc_chat",
        "p2p",
        "ou_user",
        content="[文件附件：九一班.xlsx]",
        attachments=[{"type": "file", "file_key": "file_test", "name": "../../九一班.xlsx"}],
    )
    try:
        await adapter._dispatch(message)
        assert (tmp_path / relative).read_bytes() == incoming_bytes
        assert relative in provider.requests[0][-1].content
        assert "file_test" not in provider.requests[0][-1].content
        observation = json.loads(provider.requests[1][-1].content)
        assert observation["rows"][1] == ["090101", "张明", "九一班"]
        assert observation["total_rows"] == 2 and observation["total_columns"] == 3
        assert requests[-1].method == "POST"
        assert "已读取" in json.loads(json.loads(requests[-1].content)["content"])["text"]
    finally:
        await adapter.stop()
        await agent.sandbox_router.stop()


async def test_download_failure_does_not_create_file_or_ask_llm_to_guess(tmp_path):
    texts = []

    def handle(request):
        if request.method == "GET":
            return httpx.Response(
                400,
                json={
                    "code": 99991672,
                    "msg": "Missing im:resource",
                    "error": None,
                },
            )
        texts.append(json.loads(json.loads(request.content)["content"])["text"])
        return httpx.Response(200, json={"code": 0})

    adapter = adapter_for(handle)
    provider = ScriptedProvider([])
    agent = CodingAgent(provider, workspace_dir=str(tmp_path), register_default_tools=False)
    server = object.__new__(XClawServer)
    server.config = Config(agent={
        "user_workspace_dir": tmp_path / "users", "workspace_dir": str(tmp_path / "deployment"),
    })
    server.im_adapter = adapter
    server._session_locks = {}
    server._sessions_dir = tmp_path / "sessions"
    server._get_or_create_agent = lambda *_: agent
    adapter.on_message(server._handle_im_message)
    target = tmp_path / "received.xlsx"
    try:
        assert not await adapter.download_file("om_original", "file_test", target)
        assert not target.exists()
        assert "im:resource" in adapter.file_failure_reason()
        assert "99991672" in adapter.file_failure_reason()
        await adapter._dispatch(
            IMMessage(
                "feishu",
                "om_original",
                "oc_chat",
                "p2p",
                "ou_user",
                content="[文件附件]",
                attachments=[{"file_key": "file_test", "name": "received.xlsx"}],
            )
        )
        assert provider.requests == []
        assert len(texts) == 1 and "99991672" in texts[0] and "im:resource" in texts[0]
    finally:
        await adapter.stop()
        await agent.sandbox_router.stop()


async def test_download_size_limit_preserves_existing_file_and_cleans_temporary(
    tmp_path, monkeypatch
):
    monkeypatch.setattr("xclaw.im.delivery.MAX_FILE_BYTES", 4)
    adapter = adapter_for(lambda _: httpx.Response(200, content=b"0123456789"))
    target = tmp_path / "received.xlsx"
    target.write_bytes(b"existing")
    try:
        assert not await adapter.download_file("om_original", "file_test", target)
        assert target.read_bytes() == b"existing"
        assert list(tmp_path.iterdir()) == [target]
    finally:
        await adapter.stop()
