"""服务和 Demo 的退出路径必须释放执行后端。"""

import asyncio
import signal
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from xclaw.server.config import Config
from xclaw.server.main import XClawServer, run_demo


def make_server(*agents):
    server = object.__new__(XClawServer)
    server.im_adapter = None
    server.mcp = SimpleNamespace(start=AsyncMock(), stop=AsyncMock(), attach_tools=Mock())
    server.agent = agents[0] if agents else None
    server._sessions = {str(index): agent for index, agent in enumerate(agents)}
    server._shutdown_event = asyncio.Event()
    server._stop_lock = asyncio.Lock()
    server._stopped = False
    return server


def make_agent(error=None):
    return SimpleNamespace(
        stop=Mock(),
        tool_registry=[],
        sandbox_router=SimpleNamespace(stop=AsyncMock(side_effect=error)),
    )


async def test_service_stop_wakes_start_and_deduplicates_agents(monkeypatch):
    monkeypatch.setattr(signal, "signal", lambda *args: None)
    agent = make_agent()
    server = make_server(agent)
    task = asyncio.create_task(server.start())
    await asyncio.sleep(0)
    assert not task.done()
    await server.stop()
    await asyncio.wait_for(task, timeout=1)
    await server.stop()
    assert server._shutdown_event.is_set()
    agent.sandbox_router.stop.assert_awaited_once()
    agent.stop.assert_called_once()


async def test_one_cleanup_failure_does_not_skip_other_backends():
    failing = make_agent(RuntimeError("cleanup failed"))
    healthy = make_agent()
    server = make_server(failing, healthy)
    with pytest.raises(RuntimeError, match="cleanup failed"):
        await server.stop()
    failing.sandbox_router.stop.assert_awaited_once()
    healthy.sandbox_router.stop.assert_awaited_once()
    assert server._shutdown_event.is_set()
    assert not server._stopped


@pytest.mark.parametrize("exit_action", ["quit", "interrupt", "eof", "cancel"])
async def test_demo_uses_config_and_releases_backend(monkeypatch, tmp_path, exit_action):
    agent = make_agent()
    agent.tool_registry = []
    options = {}

    def factory(**kwargs):
        options.update(kwargs)
        return agent

    def input_action(*args):
        if exit_action == "quit":
            return "/quit"
        if exit_action == "interrupt":
            raise KeyboardInterrupt
        if exit_action == "eof":
            raise EOFError
        raise asyncio.CancelledError

    monkeypatch.setattr("xclaw.coding.CodingAgent", factory)
    monkeypatch.setattr("xclaw.ai.provider.OpenAIProvider", lambda **kwargs: object())
    monkeypatch.setattr("builtins.input", input_action)
    config = Config()
    config.agent.workspace_dir = str(tmp_path)
    config.agent.execution_mode = "edit"
    config.agent.execution_timeout = 12
    if exit_action == "cancel":
        with pytest.raises(asyncio.CancelledError):
            await run_demo(config)
    else:
        await run_demo(config)
    assert options["workspace_dir"] == str(tmp_path)
    assert options["execution_policy"].mode.value == "edit"
    assert options["execution_policy"].max_timeout == 12
    assert options["enable_context_compression"] == config.agent.enable_context_compression
    assert options["max_tokens"] == config.agent.max_tokens
    assert options["context_window"] == config.providers[0].context_window
    assert options["compression_keep_recent"] == config.agent.compression_keep_recent
    agent.stop.assert_called_once()
    agent.sandbox_router.stop.assert_awaited_once()
