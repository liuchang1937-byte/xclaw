import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from xclaw.core.tool import ToolRegistry
from xclaw.im.adapter import IMMessage
from xclaw.server.main import XClawServer
from xclaw.server.schedule_config import SchedulerConfig
from xclaw.server.scheduler import Scheduler


def message(text, event="m1", sender="owner", chat_type="p2p"):
    return IMMessage("feishu", event, "private-chat", chat_type, sender, content=text)


async def test_chat_reminder_survives_restart_and_delivers_only_to_owner(tmp_path):
    server = object.__new__(XClawServer)
    server._supports_chat_schedules = True
    server._session_locks = {}
    send = AsyncMock(return_value=True)
    server.im_adapter = SimpleNamespace(send_message=send)
    run_agent = AsyncMock(side_effect=AssertionError("plain reminders need no model"))
    settings = SchedulerConfig(state_dir=str(tmp_path))

    def start():
        server.scheduler = Scheduler(settings, run_agent, server._send_scheduled_chat)
        server.scheduler.start()

    start()
    text = "每天晚上 6 点提醒我写日报"
    registry = ToolRegistry()

    async def create(prompt):
        assert prompt == text
        await registry.execute("schedule_create", json.dumps({
            "daily_at": "18:00", "prompt": "写日报", "reminder": True,
        }))
        return "已创建"

    server._build_agent = Mock(return_value=SimpleNamespace(
        run=create, tool_registry=registry, sandbox_router=SimpleNamespace(stop=AsyncMock()),
    ))
    try:
        help_text = await server._handle_im_message(message("你可以做定时任务吗"))
        assert "/schedule daily" in help_text
        server._build_agent.assert_not_called()
        result = await server._handle_im_message(message(text))
        assert "已安排定时任务" in result and "18:00（北京时间）" in result
        server._build_agent.assert_called_once_with(schedule_dialog=True)
        await server._handle_im_message(message(text))  # Same Feishu event is idempotent.
        catalog = json.loads((tmp_path / "chat-jobs.json").read_bytes())
        job_id, = catalog
        assert await server._handle_im_message(message("/schedule list", sender="other")) == (
            "当前私聊还没有创建定时任务。"
        )
        assert "请在飞书私聊" in await server._handle_im_message(
            message("/schedule list", chat_type="group")
        )
        assert "没有这个" in await server._handle_im_message(
            message(f"/schedule cancel {job_id}", sender="other")
        )
    finally:
        await server.scheduler.stop()

    path = tmp_path / "chat" / f"job-{job_id}.json"
    state = json.loads(path.read_bytes())
    state["next_run_at"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    path.write_text(json.dumps(state), encoding="utf-8")
    start()
    try:
        async with asyncio.timeout(4):
            while not send.await_count:
                await asyncio.sleep(0.01)
        send.assert_awaited_once_with("private-chat", f"定时任务 {job_id}\n写日报", chat_type="p2p")
        run_agent.assert_not_awaited()
        assert "已取消" in await server._handle_im_message(message(f"/schedule cancel {job_id}"))
    finally:
        await server.scheduler.stop()
    start()
    try:
        assert job_id not in await server._handle_im_message(message("/schedule list"))
        assert json.loads((tmp_path / "chat-jobs.json").read_bytes()) == {}
    finally:
        await server.scheduler.stop()
