import asyncio
import json
import sys

import httpx

from xclaw.im.qq_bridge import QQBridgeAdapter
from xclaw.im.weixin import WeixinAdapter, login_weixin


async def test_qq_bridge_authenticates_deduplicates_and_runs_reply_command(tmp_path):
    output = tmp_path / "reply.json"
    script = (
        "import json, os, pathlib, sys; pathlib.Path(sys.argv[1]).write_text(json.dumps(["
        "os.environ['MICROCLAW_QQ_TARGET'], os.environ['MICROCLAW_QQ_TEXT'], "
        "os.environ['XCLAW_QQ_CHAT_TYPE'], os.environ['XCLAW_QQ_REPLY_TO']]))"
    )
    bridge = QQBridgeAdapter(
        host="127.0.0.1", port=0, token="test-secret",
        send_command=[sys.executable, "-c", script, str(output)],
    )
    received = []

    @bridge.on_message
    async def reply(message):
        received.append(message)
        return "处理完毕"

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=bridge.app),
                                 base_url="http://bridge") as client:
        payload = {"user_id": "7", "chat_id": "8", "text": "运行测试", "message_id": "9"}
        assert (await client.post("/qq/events", json=payload)).status_code == 401
        headers = {"X-QQ-Webhook-Token": "test-secret"}
        assert (await client.post("/qq/events", json=payload, headers=headers)).status_code == 202
        duplicate = await client.post("/qq/events", json=payload, headers=headers)
        assert duplicate.json() == {"status": "duplicate"}
    await bridge._dispatch(bridge._queue.get_nowait())
    assert bridge._queue.empty()
    assert [message.content for message in received] == ["运行测试"]
    assert json.loads(output.read_text()) == ["8", "处理完毕", "group", "9"]


async def test_weixin_login_saves_credentials_only_after_confirmation(tmp_path, monkeypatch):
    path = tmp_path / "weixin.json"
    async_client = httpx.AsyncClient

    def respond(request):
        assert "authorization" not in request.headers
        assert not path.exists()
        if request.url.path.endswith("get_bot_qrcode"):
            assert request.method == "POST"
            assert request.url.params["bot_type"] == "3"
            return httpx.Response(200, json={"qrcode": "qr-id", "qrcode_img_content": "qr-data"})
        assert request.url.params["qrcode"] == "qr-id"
        return httpx.Response(200, json={
            "status": "confirmed", "bot_token": "test-token", "ilink_bot_id": "bot",
            "ilink_user_id": "owner",
        })

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: async_client(
        transport=httpx.MockTransport(respond), **kw,
    ))
    await login_weixin(path)
    state = json.loads(path.read_bytes())
    assert (state["account_id"], state["bot_token"], state["owner_id"]) == (
        "bot", "test-token", "owner",
    )


async def test_weixin_poll_resumes_cursor_and_replies_with_inbound_context(tmp_path, monkeypatch):
    path = tmp_path / "weixin.json"
    path.write_text(json.dumps({
        "account_id": "bot", "bot_token": "test-token", "owner_id": "owner", "cursor": "old",
    }))
    async_client = httpx.AsyncClient
    next_poll = asyncio.Event()
    sent = []

    async def respond(request):
        assert request.headers["authorization"] == "Bearer test-token"
        payload = json.loads(request.content)
        if request.url.path.endswith("sendmessage"):
            sent.append(payload["msg"])
            return httpx.Response(200, json={"ret": 0})
        if payload["get_updates_buf"] == "old":
            inbound = {
                "message_id": 42, "from_user_id": "owner", "message_type": 1,
                "context_token": "reply-context",
                "item_list": [{"type": 1, "text_item": {"text": "运行测试"}}],
            }
            return httpx.Response(200, json={
                "ret": 0, "msgs": [inbound, inbound, {**inbound, "from_user_id": "other"}],
                "get_updates_buf": "next",
            })
        assert payload["get_updates_buf"] == "next"
        next_poll.set()
        await asyncio.Future()

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: async_client(
        transport=httpx.MockTransport(respond), **kw,
    ))
    adapter = WeixinAdapter(path)

    @adapter.on_message
    async def reply(message):
        assert message.content == "运行测试"
        assert message.chat_id == "bot/owner"
        return "处理完毕"

    await adapter.start()
    try:
        await asyncio.wait_for(next_poll.wait(), timeout=2)
    finally:
        await adapter.stop()
    assert len(sent) == 1
    assert sent[0]["context_token"] == "reply-context"
    assert sent[0]["to_user_id"] == "owner"
    assert sent[0]["item_list"][0]["text_item"]["text"] == "处理完毕"
    state = json.loads(path.read_bytes())
    assert (state["cursor"], state["recent"]) == ("next", ["owner/42"])
