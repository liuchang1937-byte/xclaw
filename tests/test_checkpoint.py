"""Failure injection for durable rounds, tool results and interrupted side effects."""

import asyncio
from pathlib import Path

import pytest

from tests.test_tool_scheduling import SchedulingProvider, call, plan, read_tool
from xclaw.ai.types import ChatMessage, ChatResponse
from xclaw.coding import CodingAgent
from xclaw.core.checkpoint import CheckpointStore, UncertainToolExecutionError
from xclaw.core.execution import Capability
from xclaw.core.scheduling import SchedulingConfig
from xclaw.core.tool import Tool
from xclaw.persistence import atomic_text


class ScriptProvider(SchedulingProvider):
    async def chat(self, messages, tools=None, **kwargs):
        from xclaw.core.scheduling import PLANNING_PROMPT

        if messages[0].content == PLANNING_PROMPT:
            return await super().chat(messages, tools, **kwargs)
        self.requests.append(([m.model_copy(deep=True) for m in messages], tools, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def bot(tmp_path, provider, *, register_default_tools=False, **kwargs):
    agent = CodingAgent(
        provider,
        model="fake",
        workspace_dir=str(tmp_path),
        checkpoint_dir=str(tmp_path / "checkpoints"),
        register_default_tools=register_default_tools,
        tool_scheduling=SchedulingConfig(enabled=False),
        **kwargs,
    )
    agent.client.MAX_RETRIES = 1
    return agent


async def test_restart_after_round_keeps_history_summary_and_does_not_repeat_write(tmp_path):
    provider = ScriptProvider(
        "",
        [
            ChatResponse(tool_calls=[call("write", "write_file", path="a.py", content="x=1\n")]),
            TimeoutError("network"),
        ],
    )
    first = bot(tmp_path, provider, register_default_tools=True, enable_context_compression=True)
    first.client.set_history([ChatMessage(role="user", content="previous requirement")])
    first.client.compressor.summary = "summary before crash"
    first.remember("language", "Python", scope=first.memory_scope)
    with pytest.raises(TimeoutError):
        await first.run("write code", tools=["write_file"])
    checkpoint_id = first.last_checkpoint_id
    state = first.checkpoint_store.load(checkpoint_id)
    assert state.phase == "ready" and state.iteration == 1
    assert state.summary == "summary before crash"
    assert [m.role for m in state.messages] == ["user", "assistant", "tool"]
    assert (tmp_path / "a.py").read_text() == "x=1\n"

    second_provider = ScriptProvider("", [ChatResponse(content="finished")])
    second = bot(
        tmp_path, second_provider, register_default_tools=True, enable_context_compression=True
    )
    assert await second.resume(checkpoint_id) == "finished"
    assert second.recall("language", scope=second.memory_scope) == "Python"
    _, tools, _ = second_provider.requests[0]
    assert [tool.name for tool in tools] == ["write_file"]
    assert second.checkpoint_store.load(checkpoint_id).iteration == 2
    assert second.client.get_history()[0].content == "previous requirement"
    # A completed task is returned from disk without another model/tool call.
    assert await second.resume(checkpoint_id) == "finished"
    assert len(second_provider.requests) == 1


async def test_uncertain_write_blocks_resume_and_inspected_result_unblocks_dependents(tmp_path):
    decision = plan("dag", ("a", []), ("b", ["a"]), ("c", ["b"]))
    provider = ScriptProvider(
        decision,
        [
            ChatResponse(
                tool_calls=[
                    call("a", "work", label="a"),
                    call("b", "work", label="b"),
                    call("c", "work", label="c"),
                ]
            )
        ],
    )
    first = bot(tmp_path, provider)
    first.tool_scheduler.config.enabled = True
    executed = []

    async def work(label: str):
        executed.append(label)
        if label == "b":
            (tmp_path / "side_effect.txt").write_text("already done")
            raise asyncio.CancelledError()
        return label

    def register(agent):
        agent.register_tool(
            Tool("work", "write", work, capabilities=frozenset({Capability.FILE_WRITE}))
        )

    register(first)
    with pytest.raises(asyncio.CancelledError):
        await first.run("do three steps")
    checkpoint_id = first.last_checkpoint_id
    state = first.checkpoint_store.load(checkpoint_id)
    assert state.started == ["a", "b"] and set(state.results) == {"a"}

    second_provider = ScriptProvider("planner must not run", [ChatResponse(content="done")])
    second = bot(tmp_path, second_provider)
    register(second)
    with pytest.raises(UncertainToolExecutionError, match="b .work"):
        await second.resume(checkpoint_id)
    assert second_provider.requests == [] and executed == ["a", "b"]
    assert await second.resume(checkpoint_id, resolutions={"b": "confirmed complete"}) == "done"
    assert executed == ["a", "b", "c"]
    messages = second_provider.requests[0][0]
    assert [m.tool_call_id for m in messages if m.role == "tool"] == ["a", "b", "c"]


async def test_uncertain_read_retries_and_preserves_failed_dependency_skip(tmp_path):
    decision = plan("dag", ("a", []), ("b", ["a"]), ("c", []))
    provider = ScriptProvider(
        decision, [ChatResponse(tool_calls=[call(k, label=k) for k in ("a", "b", "c")])]
    )
    first = bot(tmp_path, provider)
    first.tool_scheduler.config.enabled = True
    calls = []
    interrupted = False

    async def read(label: str):
        nonlocal interrupted
        calls.append(label)
        if label == "a":
            return "❌ unavailable"
        if label == "c" and not interrupted:
            interrupted = True
            raise asyncio.CancelledError()
        return label

    first.register_tool(read_tool(read))
    with pytest.raises(asyncio.CancelledError):
        await first.run("read")
    second_provider = ScriptProvider("", [ChatResponse(content="cannot finish b")])
    second = bot(tmp_path, second_provider)
    second.register_tool(read_tool(read))
    assert await second.resume(first.last_checkpoint_id) == "cannot finish b"
    assert calls.count("a") == 1 and calls.count("c") == 2 and "b" not in calls
    results = [m.content for m in second_provider.requests[0][0] if m.role == "tool"]
    assert "前置调用失败" in results[1]


async def test_checkpoint_write_failure_does_not_start_tool(tmp_path, monkeypatch):
    first = bot(tmp_path, ScriptProvider("", [ChatResponse(tool_calls=[call("a")])]))
    executed = []

    async def read():
        executed.append(True)
        return "ok"

    first.register_tool(read_tool(read))
    save = first.checkpoint_store.save

    def fail_intent(state):
        if state.started:
            raise OSError("disk full")
        save(state)

    monkeypatch.setattr(first.checkpoint_store, "save", fail_intent)
    with pytest.raises(OSError, match="disk full"):
        await first.run("read")
    assert executed == []


async def test_delivery_queue_survives_context_compaction_and_is_revalidated(tmp_path):
    from xclaw.im.delivery import collect_deliveries, register_delivery_tool
    from xclaw.server.main import _restore_checkpoint_deliveries

    output = tmp_path / "report.txt"
    output.write_text("final report")
    first = bot(
        tmp_path,
        ScriptProvider(
            "",
            [
                ChatResponse(tool_calls=[call("send", "deliver_file", path="report.txt")]),
                TimeoutError(),
            ],
        ),
    )
    register_delivery_tool(first.tool_registry)
    with collect_deliveries() as files:
        with pytest.raises(TimeoutError):
            await first.run("deliver report")
        assert files == [output]
    state = first.checkpoint_store.load(first.last_checkpoint_id)
    assert state.deliveries == [str(output)]
    # Compression can discard older tool groups; the delivery record is independent.
    state.messages = [ChatMessage(role="user", content="deliver report")]
    restored = []
    _restore_checkpoint_deliveries(state, first, restored)
    assert restored == [output]
    output.unlink()
    with pytest.raises(ValueError, match="必须存在"):
        _restore_checkpoint_deliveries(state, first, [])


async def test_resume_rejects_wrong_session_config_corruption_and_validator_loss(tmp_path):
    first = bot(tmp_path, ScriptProvider("", [TimeoutError()]))
    first.run_metadata = {"session_id": "owner"}
    with pytest.raises(TimeoutError):
        await first.run("task", completion_validator=lambda _: None)
    checkpoint_id = first.last_checkpoint_id
    second = bot(tmp_path, ScriptProvider("", []))
    with pytest.raises(ValueError, match="会话"):
        await second.resume(checkpoint_id)
    second.run_metadata = dict(first.run_metadata)
    with pytest.raises(ValueError, match="校验器"):
        await second.resume(checkpoint_id)
    second.client.system_prompt += "changed"
    with pytest.raises(ValueError, match="配置"):
        await second.resume(checkpoint_id, completion_validator=lambda _: None)
    first.checkpoint_store.path(checkpoint_id).write_text('{"version":999}')
    with pytest.raises(ValueError):
        await first.resume(checkpoint_id, completion_validator=lambda _: None)
    with pytest.raises(ValueError, match="ID"):
        first.checkpoint_store.load("../escape")


def test_checkpoint_lease_prevents_duplicate_executor_and_releases_on_exit(tmp_path):
    store = CheckpointStore(tmp_path)
    checkpoint_id = "a" * 32
    with store.lease(checkpoint_id):
        with pytest.raises(RuntimeError, match="另一个"):
            with store.lease(checkpoint_id):
                pass
    with store.lease(checkpoint_id):
        pass


@pytest.mark.parametrize("failure_point", ["fsync", "replace"])
def test_atomic_write_preserves_original_on_failure(tmp_path, monkeypatch, failure_point):
    import xclaw.persistence as persistence

    target = tmp_path / "code.py"
    target.write_bytes(b"old intact\n")

    def fail(*args):
        raise OSError("interrupted")

    monkeypatch.setattr(persistence.os, failure_point, fail)
    with pytest.raises(OSError):
        atomic_text(target, "new code\n" * 1000)
    assert target.read_bytes() == b"old intact\n"
    assert not list(tmp_path.glob("*.tmp"))


def test_atomic_write_keeps_executable_permission(tmp_path):
    import os
    import stat

    target = tmp_path / "run.sh"
    target.write_text("old")
    target.chmod(0o755)
    mode = stat.S_IMODE(target.stat().st_mode)
    atomic_text(target, "new\n")
    assert target.read_bytes() == b"new\n"
    assert stat.S_IMODE(target.stat().st_mode) == mode
    assert os.path.isfile(target)


def test_hard_process_exit_keeps_tool_journal_and_releases_lease(tmp_path):
    import subprocess
    import sys
    import textwrap

    script = textwrap.dedent("""
        import asyncio, os, sys
        from pathlib import Path
        from tests.test_checkpoint import ScriptProvider, bot
        from tests.test_tool_scheduling import call
        from xclaw.ai.types import ChatResponse
        from xclaw.core.execution import Capability
        from xclaw.core.tool import Tool

        root = Path(sys.argv[1])
        agent = bot(root, ScriptProvider('', [ChatResponse(tool_calls=[
            call('a', 'work', label='a'), call('b', 'work', label='b')
        ])]))
        async def work(label: str):
            if label == 'b':
                (root / 'effect.txt').write_text('done')
                os._exit(23)
            return label
        agent.register_tool(Tool('work', 'write', work,
                            capabilities=frozenset({Capability.FILE_WRITE})))
        asyncio.run(agent.run('task'))
    """)
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path)], capture_output=True, timeout=30
    )
    assert result.returncode == 23, result.stderr.decode(errors="replace")
    store = CheckpointStore(tmp_path / "checkpoints")
    (state,) = store.list()
    assert state.phase == "tools" and state.iteration == 1
    assert set(state.results) == {"a"} and state.started == ["a", "b"]
    assert (tmp_path / "effect.txt").read_text() == "done"
    with store.lease(state.run_id):
        assert store.load(state.run_id).results["a"].content == "a"


async def test_im_resume_reuses_isolated_workspace_and_is_scoped_to_session(tmp_path):
    from types import SimpleNamespace

    from xclaw.ai.provider import ProviderRegistry
    from xclaw.im.adapter import IMMessage
    from xclaw.server.config import Config
    from xclaw.server.main import XClawServer

    source = tmp_path / "source"
    source.mkdir()
    (source / "a.py").write_text("old\n")
    server = object.__new__(XClawServer)
    server.config = Config(
        providers=[{"name": "fake", "model": "fake"}],
        default_provider="fake",
        agent={
            "workspace_dir": str(source),
            "user_workspace_dir": str(tmp_path / "users"),
            "isolate_workspace": True,
            "task_workspace_dir": str(tmp_path / "tasks"),
            "checkpoint_dir": str(tmp_path / "checkpoints"),
            "auto_memory": False,
            "enable_context_compression": False,
            "trace_dir": None,
            "tool_scheduling": {"enabled": False},
        },
    )
    provider = ScriptProvider(
        "",
        [
            ChatResponse(tool_calls=[call("write", "write_file", path="a.py", content="new\n")]),
            TimeoutError("network"),
            ChatResponse(content="finished"),
        ],
    )
    # Avoid consuming the scripted completion on client-level retries.
    server.provider_registry = ProviderRegistry()
    server.provider_registry.register("fake", provider)
    server.event_bus = None
    server.mcp = SimpleNamespace(attach_tools=lambda registry: None)
    server.im_adapter = SimpleNamespace(supports_files=False)
    server._sessions, server._session_locks = {}, {}
    server._sessions_dir = tmp_path / "sessions"
    build = server._build_agent

    def build_once(workspace_dir=None, **kwargs):
        agent = build(workspace_dir, **kwargs)
        agent.client.MAX_RETRIES = 1
        return agent

    server._build_agent = build_once

    def message(text, sender="owner"):
        return IMMessage("feishu", "m1", "chat", "p2p", sender, content=text)

    owner_id = server._session_id(message(""))
    work = server._workspace_scope(owner_id) / "work"
    work.mkdir()
    (work / "a.py").write_text("old\n")
    failed = await server._handle_im_message(message("edit a.py"))
    assert "可恢复 checkpoint" in failed
    (state,) = CheckpointStore(tmp_path / "checkpoints").list()
    assert (source / "a.py").read_text() == "old\n"
    assert state.run_id in await server._handle_im_message(message("/checkpoints"))
    rejected = await server._handle_im_message(message(f"/resume {state.run_id}", "other"))
    assert "不属于当前会话" in rejected
    # Simulate a service restart, retaining only the persisted state and workspace.
    for agent in server._sessions.values():
        await agent.sandbox_router.stop()
    server._sessions.clear()
    resumed = await server._handle_im_message(message(f"/resume {state.run_id}"))
    assert "finished" in resumed and "变更记录" in resumed
    assert len(list((tmp_path / "tasks").iterdir())) == 1
    assert (source / "a.py").read_text() == "old\n"
    assert (Path(state.workspace) / "a.py").read_text() == "new\n"
    assert len(server._sessions[owner_id].client.get_history()) == 2
    await server._sessions[owner_id].sandbox_router.stop()
