import json
import os
from types import SimpleNamespace
from unittest.mock import Mock

from tests.test_evaluation import FixProvider
from xclaw.ai.provider import ProviderRegistry
from xclaw.evaluation.tasks import builtin_tasks
from xclaw.im.adapter import IMMessage
from xclaw.server.config import Config
from xclaw.server.main import XClawServer


async def test_im_task_modifies_only_copy_preserves_history_and_exports_patch(tmp_path):
    task = builtin_tasks()[0]
    source = tmp_path / "source"
    source.mkdir()
    (source / "app.py").write_text(task.seed["app.py"])
    server = object.__new__(XClawServer)
    server.config = Config(
        providers=[{"name": "fake", "model": "fake"}],
        default_provider="fake",
        agent={
            "workspace_dir": str(source),
            "user_workspace_dir": str(tmp_path / "users"),
            "isolate_workspace": True,
            "task_workspace_dir": str(tmp_path / "tasks"),
            "auto_memory": False,
            "enable_context_compression": False,
            "trace_dir": str(tmp_path / "traces"),
            "checkpoint_dir": str(tmp_path / "checkpoints"),
        },
    )
    server.provider_registry = ProviderRegistry()
    server.provider_registry.register("fake", FixProvider(task))
    server.event_bus = None
    server.mcp = SimpleNamespace(attach_tools=lambda registry: None)
    server.im_adapter = SimpleNamespace(supports_files=False)
    server._sessions, server._session_locks = {}, {}
    server._sessions_dir = tmp_path / "sessions"
    message = IMMessage("feishu", "msg1", "chat", "p2p", "user", content=task.prompt)
    session_id = server._session_id(message)
    private = server._workspace_scope(session_id) / "work"
    private.mkdir()
    (private / "app.py").write_text(task.seed["app.py"])
    reply = await server._handle_im_message(message)
    assert "原工作区尚未回写" in reply
    assert (source / "app.py").read_text() == task.seed["app.py"]
    work = next((tmp_path / "tasks").glob("*/work"))
    assert (work / "app.py").read_text() == task.reference["app.py"]
    assert "+    if low > high" in (work.parent / "changes.diff").read_text()
    assert (private / "app.py").read_text() == task.seed["app.py"]
    session = server._sessions[session_id]
    assert len(session.client.get_history()) == 2
    assert (server._sessions_dir / f"{session_id}.json").exists()
    events = [
        json.loads(line)
        for trace in (tmp_path / "traces").glob("*.jsonl")
        for line in trace.read_text().splitlines()
    ]
    assert any(
        event["event"] == "run.started" and event["session_id"] == session_id
        for event in events
    )
    await session.sandbox_router.stop()


async def test_im_users_keep_separate_files_and_history_across_restart(tmp_path):
    server = object.__new__(XClawServer)
    server.config = Config(
        providers=[{"name": "fake", "model": "fake"}],
        default_provider="fake",
        agent={
            "workspace_dir": str(tmp_path / "deployment"),
            "user_workspace_dir": str(tmp_path / "users"),
            "execution_mode": "edit",
        },
    )
    server.mcp = SimpleNamespace(attach_tools=Mock())
    server.provider_registry = ProviderRegistry()
    server.provider_registry.register("fake", FixProvider(builtin_tasks()[0]))
    server.event_bus = None
    server.im_adapter = SimpleNamespace(supports_files=False)
    server._sessions, server._session_locks = {}, {}
    server._sessions_dir = tmp_path / "sessions"
    server._sessions_dir.mkdir()
    (server._sessions_dir / "private-alice.json").write_text("{}", encoding="utf-8")

    def msg(sender, chat="dm", kind="p2p", platform="feishu"):
        return IMMessage(platform, "message", chat, kind, sender, content="/status")

    async def tool(agent, name, **params):
        return await agent.tool_registry.execute(name, json.dumps(params))

    alice, bob = msg("alice"), msg("bob")
    alice_id, bob_id = server._session_id(alice), server._session_id(bob)
    assert server._session_id(msg("alice", "later-chat")) == alice_id
    assert server._session_id(msg("alice", platform="qq")) != alice_id
    assert server._session_id(msg("alice", "group", "group")) != alice_id
    assert server._session_id(msg("alice", "group", "group")) != server._session_id(
        msg("bob", "group", "group")
    )
    assert "无法确认" in await server._handle_im_message(msg(""))
    assert "可用工具" in await server._handle_im_message(alice)
    assert "可用工具" in await server._handle_im_message(bob)
    left, right = server._sessions[alice_id], server._sessions[bob_id]
    assert left.workspace_dir != right.workspace_dir
    server.mcp.attach_tools.assert_not_called()
    assert "文件已写入" in await tool(left, "write_file", path="data.txt", content="ALICE ONLY")
    assert "文件已写入" in await tool(right, "write_file", path="data.txt", content="BOB ONLY")
    other = os.path.join(left.workspace_dir, "data.txt")
    for path in (other, os.path.relpath(other, right.workspace_dir)):
        assert "超出工作区" in await tool(right, "read_file", path=path)
        assert "超出工作区" in await tool(right, "write_file", path=path, content="bad")
    try:
        os.symlink(other, os.path.join(right.workspace_dir, "external"))
    except OSError:
        if os.name != "nt":
            raise
    else:
        assert "超出工作区" in await tool(right, "read_file", path="external")
    for agent in (left, right):
        await agent.sandbox_router.stop()
    server._sessions.clear()
    assert "可用工具" in await server._handle_im_message(alice)
    reopened = server._sessions[alice_id]
    assert "ALICE ONLY" in await tool(reopened, "read_file", path="data.txt")
    await reopened.sandbox_router.stop()
