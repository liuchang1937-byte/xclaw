import json
from types import SimpleNamespace

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
    reply = await server._handle_im_message(message)
    assert "原工作区尚未回写" in reply
    assert (source / "app.py").read_text() == task.seed["app.py"]
    work = next((tmp_path / "tasks").glob("*/work"))
    assert (work / "app.py").read_text() == task.reference["app.py"]
    assert "+    if low > high" in (work.parent / "changes.diff").read_text()
    session = server._sessions["private-user"]
    assert len(session.client.get_history()) == 2
    assert (server._sessions_dir / "private-user.json").exists()
    events = [
        json.loads(line)
        for trace in (tmp_path / "traces").glob("*.jsonl")
        for line in trace.read_text().splitlines()
    ]
    assert any(
        event["event"] == "run.started" and event["session_id"] == "private-user"
        for event in events
    )
    await session.sandbox_router.stop()
