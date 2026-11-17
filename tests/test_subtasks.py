import asyncio
import hashlib
import json
from types import SimpleNamespace

from xclaw.ai.provider import BaseProvider, ProviderRegistry
from xclaw.ai.types import ChatMessage, ChatResponse, FunctionCall, ToolCall
from xclaw.im.adapter import IMMessage, IMReply
from xclaw.server.config import Config
from xclaw.server.main import XClawServer
from xclaw.server.subtasks import SubtaskStore


class TaskProvider(BaseProvider):
    def __init__(self, *, cycle=False):
        self.cycle = cycle
        self.active_readers = set()
        self.parallel = asyncio.Event()
        self.unblock = asyncio.Event()
        self.interrupt = True
        self.write_calls = 0
        self.plan_calls = 0

    @staticmethod
    def answer(summary, files=None):
        return ChatResponse(content=json.dumps({
            "ok": True, "summary": summary, "files": files or [],
        }))

    async def chat(self, messages, tools=None, **kwargs):
        user = next(message.content for message in messages if message.role == "user")
        assert "old-conversation" not in user
        if not tools:
            self.plan_calls += 1
            assert "准备报告" in user
            return ChatResponse(content=json.dumps({"steps": [
                {"id": "read-a", "title": "A", "instruction": "scan-a", "mode": "read",
                 "depends_on": ["write"] if self.cycle else []},
                {"id": "read-b", "title": "B", "instruction": "scan-b", "mode": "read"},
                {"id": "write", "title": "写报告", "instruction": "edit", "mode": "write",
                 "depends_on": ["read-a"]},
            ]}))
        task = json.loads(user)
        names = {tool.name for tool in tools}
        instruction = task["步骤"]
        if instruction in ("scan-a", "scan-b"):
            assert "write_file" not in names and "bash" not in names and "read_file" in names
            assert "准备报告" not in user
            self.active_readers.add(instruction)
            if len(self.active_readers) == 2:
                self.parallel.set()
            await self.unblock.wait()
            return self.answer(instruction + "-handoff")
        if instruction == "edit":
            assert set(task["前置结论"]) == {"read-a"}
            assert "scan-a-handoff" in user and "scan-b-handoff" not in user
            if not any(message.role == "tool" for message in messages):
                self.write_calls += 1
                return ChatResponse(tool_calls=[ToolCall(
                    id="write-report",
                    function=FunctionCall(
                        name="write_file",
                        arguments=json.dumps({"path": "report.txt", "content": "fixture output"}),
                    ),
                )])
            if self.interrupt:
                self.interrupt = False
                raise TimeoutError("fixture interruption")
            return self.answer("报告已生成", ["report.txt"])
        assert "write_file" not in names and "bash" not in names
        assert "准备报告" in task["用户要求"] and "报告已生成" in user
        return self.answer("验收完成", ["report.txt"])

    async def chat_stream(self, *_args, **_kwargs):
        raise AssertionError("子任务不使用流式调用")
        yield


def _server(tmp_path, provider, *, mode="edit"):
    server = object.__new__(XClawServer)
    server.config = Config(
        providers=[{"name": "fixture", "model": "fixture"}], default_provider="fixture",
        im={"user_execution_modes": {"feishu": {"owner": mode}}},
        agent={
            "workspace_dir": str(tmp_path / "deployment"),
            "user_workspace_dir": str(tmp_path / "users"),
            "execution_mode": mode,
            "trace_dir": None, "auto_memory": False,
            "checkpoint_dir": None,  # The orchestrated run owns its checkpoints.
            "tool_scheduling": {"enabled": False},
        },
    )
    server.provider_registry = ProviderRegistry()
    server.provider_registry.register("fixture", provider)
    server.mcp = SimpleNamespace(attach_tools=lambda _: None)
    server.event_bus = None
    server.im_adapter = SimpleNamespace(supports_files=True)
    server._sessions, server._session_locks = {}, {}
    server._sessions_dir = tmp_path / "sessions"
    build = server._build_agent

    def build_worker(*args, **kwargs):
        agent = build(*args, **kwargs)
        agent.client.MAX_RETRIES = 1
        return agent

    server._build_agent = build_worker
    return server


def _message(content, sender="owner", event="create-event"):
    return IMMessage("feishu", event, "private-chat", "p2p", sender, content=content)


async def test_task_uses_fresh_context_and_resumes_completed_tool_without_replaying(tmp_path):
    provider = TaskProvider()
    server = _server(tmp_path, provider)
    message = _message("/task 准备报告并把文件发给我")
    scope_id = server._session_id(message)
    await server._handle_im_message(_message("/status"))
    main_agent = server._sessions[scope_id]
    main_agent.client.set_history([
        ChatMessage(role="user", content="old-conversation"),
        ChatMessage(role="assistant", content="old-conversation"),
    ])
    job_id = hashlib.sha256(message.message_id.encode()).hexdigest()[:16]
    running = asyncio.create_task(server._handle_im_message(message))
    try:
        await asyncio.wait_for(provider.parallel.wait(), timeout=5)
        status = await asyncio.wait_for(
            server._handle_im_message(_message(f"/task status {job_id}")), timeout=1,
        )
        assert "进行中 · read-a" in status and "进行中 · read-b" in status
    finally:
        provider.unblock.set()
        first = await running
    assert f"/task resume {job_id}" in first
    assert "失败 · write" in first and provider.write_calls == 1
    assert "找不到" in await server._handle_im_message(
        _message(f"/task resume {job_id}", sender="outsider")
    )
    owner = server._workspace_scope(scope_id)
    store = SubtaskStore(owner / "subtasks")
    assert (owner / "work" / "report.txt").read_text() == "fixture output"
    assert not store.load(job_id).completed
    await main_agent.sandbox_router.stop()
    # A new app instance reads the same owner-scoped state from disk.
    restarted = _server(tmp_path, provider)
    reply = await restarted._handle_im_message(_message(f"/task resume {job_id}", event="resume"))
    assert isinstance(reply, IMReply) and "已验收" in reply.text
    assert reply.files[0].read_text() == "fixture output"
    reply.staging.cleanup()
    assert provider.write_calls == 1 and provider.plan_calls == 1
    assert store.load(job_id).completed
    assert "已验收" in await restarted._handle_im_message(message)
    for agent in (*server._sessions.values(), *restarted._sessions.values()):
        await agent.sandbox_router.stop()


async def test_task_rejects_cyclic_plan_before_worker_execution(tmp_path):
    provider = TaskProvider(cycle=True)
    server = _server(tmp_path, provider)
    response = await server._handle_im_message(_message("/task 准备报告"))
    assert "循环依赖" in response
    assert not provider.active_readers and not provider.write_calls
    for agent in server._sessions.values():
        await agent.sandbox_router.stop()


async def test_task_write_worker_keeps_read_only_chat_permissions(tmp_path):
    provider = TaskProvider()
    provider.unblock.set()
    server = _server(tmp_path, provider, mode="view")
    message = _message("/task 准备报告")

    reply = await server._handle_im_message(message)

    assert "当前账号为只读" in reply and provider.write_calls == 0
    scope = server._workspace_scope(server._session_id(message))
    assert not (scope / "work" / "report.txt").exists()
    for agent in server._sessions.values():
        await agent.sandbox_router.stop()
