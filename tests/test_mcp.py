"""MCP 的连接、权限、分页与真实 stdio/HTTP 协议验证。"""

import asyncio
import json
import socket
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import uvicorn
from mcp import types
from mcp.server.fastmcp import FastMCP

from xclaw.core.tool import ToolRegistry
from xclaw.mcp.client import MCPClientManager, format_result, tool_name
from xclaw.mcp.config import MCPServerConfig, expand_env
from xclaw.sandbox import ExecutionPolicy, SandboxRouter

SCHEMA = {
    "type": "object",
    "properties": {"data": {"$ref": "#/$defs/student"}},
    "required": ["data"],
    "additionalProperties": False,
    "$defs": {
        "student": {
            "type": "object",
            "properties": {"grade": {"type": "integer", "minimum": 1}},
            "required": ["grade"],
            "additionalProperties": False,
        }
    },
}


@pytest.fixture
def sdk(monkeypatch):
    state = SimpleNamespace(entered=[], exited=[], sessions=[], params=[])

    @asynccontextmanager
    async def transport(params):
        state.params.append(params)
        owner = asyncio.current_task()
        state.entered.append(params.command)
        try:
            yield params.command, None
        finally:
            assert asyncio.current_task() is owner
            state.exited.append(params.command)

    @asynccontextmanager
    async def session(read, write, **kwargs):
        async def initialize():
            if read == "bad":
                raise RuntimeError("failed")
            if read == "slow":
                await asyncio.Event().wait()

        async def list_tools(cursor=None):
            if cursor is None:
                return types.ListToolsResult(
                    tools=[
                        types.Tool(
                            name="first",
                            description="first",
                            inputSchema={"type": "object"},
                        )
                    ],
                    nextCursor="second-page",
                )
            return types.ListToolsResult(
                tools=[
                    types.Tool(
                        name="student",
                        description="student",
                        inputSchema=SCHEMA,
                    )
                ]
            )

        value = SimpleNamespace(
            initialize=AsyncMock(side_effect=initialize),
            list_tools=AsyncMock(side_effect=list_tools),
            call_tool=AsyncMock(
                return_value=types.CallToolResult(
                    content=[types.TextContent(type="text", text="student-result")],
                )
            ),
        )
        state.sessions.append(value)
        owner = asyncio.current_task()
        try:
            yield value
        finally:
            assert asyncio.current_task() is owner

    monkeypatch.setattr("xclaw.mcp.client.stdio_client", transport)
    monkeypatch.setattr("xclaw.mcp.client.ClientSession", session)
    return state


async def test_paginated_discovery_filter_validation_and_policy(sdk, tmp_path):
    manager = MCPClientManager(
        [
            MCPServerConfig(
                name="school",
                command="test",
                allowed_tools=["student"],
            )
        ]
    )
    registry = ToolRegistry()
    router = SandboxRouter(str(tmp_path))
    registry.set_sandbox_router(router)
    try:
        await manager.start()
        await manager.start()
        manager.attach_tools(registry)
        manager.attach_tools(registry)
        name = tool_name("school", "student")
        assert registry.list_tools() == [name]
        assert await registry.execute(name, '{"data":{"grade":9}}') == "student-result"
        assert sdk.sessions[0].call_tool.call_args.args == ("student",)
        assert sdk.sessions[0].call_tool.call_args.kwargs == {"arguments": {"data": {"grade": 9}}}
        assert "Schema" in await registry.execute(name, '{"data":{"grade":"9"}}')
        assert sdk.sessions[0].call_tool.await_count == 1
        router.policy = ExecutionPolicy(mode="view")
        assert registry.list_tools() == []
        assert "拒绝" in await registry.execute(name, '{"data":{"grade":9}}')
        assert sdk.sessions[0].call_tool.await_count == 1
        # 由其他任务发起退出，SDK 上下文仍由原连接任务关闭。
        await asyncio.create_task(manager.stop())
        assert sdk.exited == ["test"]
        router.policy = ExecutionPolicy()
        assert "未连接" in await registry.execute(name, '{"data":{"grade":9}}')
    finally:
        await manager.stop()
        await router.stop()


@pytest.mark.parametrize("second", ["bad", "slow"])
async def test_partial_startup_failure_closes_every_connection(sdk, second):
    manager = MCPClientManager(
        [
            MCPServerConfig(name="good", command="test"),
            MCPServerConfig(name="failed", command=second, timeout=0.02),
        ]
    )
    with pytest.raises(RuntimeError, match="failed.*连接失败"):
        await manager.start()
    assert set(sdk.exited) == {"test", second}
    await manager.stop()


async def test_call_timeout_does_not_destroy_connection(sdk):
    manager = MCPClientManager([MCPServerConfig(name="demo", command="test", timeout=0.02)])
    await manager.start()
    registry = ToolRegistry()
    manager.attach_tools(registry)

    async def slow(*args, **kwargs):
        await asyncio.Event().wait()

    try:
        sdk.sessions[0].call_tool.side_effect = slow
        assert "超时" in await registry.execute(
            tool_name("demo", "student"), '{"data":{"grade":9}}'
        )
        sdk.sessions[0].call_tool.side_effect = None
        assert "student-result" in await registry.execute(
            tool_name("demo", "student"), '{"data":{"grade":9}}'
        )
    finally:
        await manager.stop()


def test_config_environment_and_name_collisions(monkeypatch):
    monkeypatch.setenv("XCLAW_MCP_TEST_TOKEN", "test-value")
    assert expand_env("Bearer ${XCLAW_MCP_TEST_TOKEN}") == "Bearer test-value"
    with pytest.raises(ValueError, match="环境变量未设置"):
        expand_env("${XCLAW_MCP_MISSING_VAR}")
    with pytest.raises(ValueError):
        MCPServerConfig(name="local")
    with pytest.raises(ValueError):
        MCPServerConfig(name="remote", transport="streamable-http", url="file:///tmp/server")
    with pytest.raises(ValueError, match="名称不能重复"):
        MCPClientManager([MCPServerConfig(name="same", command="test")] * 2)
    assert tool_name("demo", "a.b") != tool_name("demo", "a_b")
    assert len(tool_name("very-long-server", "x" * 200)) <= 64


def test_error_and_structured_result_are_preserved_without_base64():
    result = types.CallToolResult(
        isError=True,
        structuredContent={"reason": "invalid"},
        content=[
            types.TextContent(type="text", text="failed"),
            types.ImageContent(type="image", data="VERY_BIG_DATA", mimeType="image/png"),
        ],
    )
    formatted = format_result(result)
    assert "错误" in formatted and "failed" in formatted and "invalid" in formatted
    assert "VERY_BIG_DATA" not in formatted


async def test_real_stdio_server_discovers_and_executes_tools():
    script = Path(__file__).resolve().parents[1] / "examples" / "mcp_demo_server.py"
    manager = MCPClientManager(
        [
            MCPServerConfig(
                name="demo",
                command=sys.executable,
                args=[str(script)],
                timeout=10,
            )
        ]
    )
    try:
        await manager.start()
        registry = ToolRegistry()
        manager.attach_tools(registry)
        assert "5" in await registry.execute(tool_name("demo", "add"), '{"a":2,"b":3}')
        result = await registry.execute(
            tool_name("demo", "echo"),
            json.dumps(
                {
                    "message": "九一班",
                    "tags": ["student"],
                }
            ),
        )
        assert "九一班" in result and "student" in result
    finally:
        await manager.stop()


async def test_real_http_server_and_environment_headers(monkeypatch):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.listen()
    mcp_server = FastMCP(
        "http-demo", host="127.0.0.1", port=port, stateless_http=True, json_response=True
    )

    @mcp_server.tool()
    def add(a: int, b: int) -> int:
        return a + b

    app = mcp_server.streamable_http_app()
    headers = []

    async def record(scope, receive, send):
        if scope["type"] == "http":
            headers.append(dict(scope["headers"]).get(b"authorization"))
        await app(scope, receive, send)

    server = uvicorn.Server(uvicorn.Config(record, log_level="error", access_log=False))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    monkeypatch.setenv("XCLAW_MCP_HTTP_TOKEN", "test-auth")
    manager = MCPClientManager(
        [
            MCPServerConfig(
                name="remote",
                transport="streamable-http",
                url=f"http://127.0.0.1:{port}/mcp",
                headers={"Authorization": "Bearer ${XCLAW_MCP_HTTP_TOKEN}"},
                timeout=5,
            )
        ]
    )
    try:
        async with asyncio.timeout(5):
            while not server.started:
                if task.done():
                    await task
                await asyncio.sleep(0.01)
        await manager.start()
        registry = ToolRegistry()
        manager.attach_tools(registry)
        assert "9" in await registry.execute(tool_name("remote", "add"), '{"a":4,"b":5}')
        assert headers and all(header == b"Bearer test-auth" for header in headers)
    finally:
        await manager.stop()
        server.should_exit = True
        await asyncio.wait_for(task, timeout=5)
        sock.close()
