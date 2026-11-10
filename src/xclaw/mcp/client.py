"""通过官方 SDK 连接 MCP；连接上下文始终在同一后台任务内打开和关闭。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from contextlib import AsyncExitStack
from datetime import timedelta
from typing import Any

import httpx
from jsonschema import ValidationError
from jsonschema.validators import validator_for
from loguru import logger
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client
from referencing import Registry

from xclaw.core.execution import Capability
from xclaw.core.tool import Tool, ToolRegistry
from xclaw.mcp.config import MCPServerConfig, expand_env


def tool_name(server: str, name: str) -> str:
    """兼容模型工具名称限制，哈希避免不同服务或清洗后的名称冲突。"""
    digest = hashlib.sha256(f"{server}\0{name}".encode()).hexdigest()[:10]
    readable = re.sub(r"[^A-Za-z0-9_-]", "_", f"mcp_{server}_{name}")
    return f"{readable[:53]}_{digest}"


def format_result(result) -> str:
    parts = []
    for content in result.content:
        if content.type == "text":
            parts.append(content.text)
        elif content.type == "resource":
            resource = content.resource
            if hasattr(resource, "text"):
                parts.append(resource.text)
            else:
                parts.append(f"[MCP 二进制资源: {resource.uri}]")
        elif content.type == "resource_link":
            parts.append(f"[{content.name}]({content.uri})")
        else:
            # 当前模型消息仅支持文本，避免把大型 base64 数据塞入上下文。
            parts.append(f"[MCP {content.type} 内容未显示，当前仅支持文本结果]")
    if result.structuredContent is not None:
        parts.append(json.dumps(result.structuredContent, ensure_ascii=False))
    text = "\n".join(parts) or "MCP 工具执行完成（无文本结果）"
    return f"错误: MCP 工具执行失败\n{text}" if result.isError else text


class MCPTool(Tool):
    """动态工具只按服务的 JSON Schema 校验，不绑定本地函数签名。"""

    def __init__(self, connection: MCPConnection, definition):
        schema = definition.inputSchema
        validator_cls = validator_for(schema)
        validator_cls.check_schema(schema)
        # 禁止参数校验主动访问远程 $ref；本地 $defs 仍正常工作。
        self._validator = validator_cls(schema, registry=Registry())

        async def call(**arguments):
            return await connection.call_tool(definition.name, arguments)

        super().__init__(
            name=tool_name(connection.config.name, definition.name),
            description=f"[MCP {connection.config.name}/{definition.name}] "
            f"{definition.description or definition.name}",
            func=call,
            parameters=schema,
            capabilities=frozenset({Capability.MCP}),
        )
        self.parameters = schema

    def bind_arguments(self, arguments: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(arguments, dict):
            raise ValueError("MCP 工具参数必须是 JSON 对象")
        try:
            self._validator.validate(arguments)
        except ValidationError as exc:
            path = ".".join(map(str, exc.absolute_path)) or "参数"
            # 不在错误里打印可能包含凭据的实例值。
            raise ValueError(f"MCP {path} 不符合 Schema（{exc.validator}）") from exc
        except Exception as exc:
            raise ValueError("MCP 工具参数 Schema 引用无法解析") from exc
        return dict(arguments)


class MCPConnection:
    def __init__(self, config: MCPServerConfig):
        self.config = config
        self.tools: list[MCPTool] = []
        self._session: ClientSession | None = None
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._error: BaseException | None = None
        self._startup_complete = False

    async def start(self) -> None:
        ready = asyncio.get_running_loop().create_future()
        self._task = asyncio.create_task(self._serve(ready), name=f"mcp-{self.config.name}")
        try:
            await ready
        except BaseException:
            self._stop.set()
            await self._task
            raise

    async def _serve(self, ready: asyncio.Future) -> None:
        try:
            async with AsyncExitStack() as stack:
                async with asyncio.timeout(self.config.timeout):
                    if self.config.transport == "stdio":
                        params = StdioServerParameters(
                            command=expand_env(self.config.command),
                            args=[expand_env(arg) for arg in self.config.args],
                            env={key: expand_env(value) for key, value in self.config.env.items()},
                            cwd=expand_env(self.config.cwd) if self.config.cwd else None,
                        )
                        read, write = await stack.enter_async_context(stdio_client(params))
                    else:
                        client = await stack.enter_async_context(
                            httpx.AsyncClient(
                                headers={k: expand_env(v) for k, v in self.config.headers.items()},
                                timeout=httpx.Timeout(self.config.timeout),
                            )
                        )
                        read, write, _ = await stack.enter_async_context(
                            streamable_http_client(
                                self.config.url,
                                http_client=client,
                            )
                        )
                    session = await stack.enter_async_context(
                        ClientSession(
                            read,
                            write,
                            read_timeout_seconds=timedelta(seconds=self.config.timeout),
                        )
                    )
                    await session.initialize()
                    definitions = []
                    cursor = None
                    cursors = set()
                    while True:
                        page = await session.list_tools(cursor=cursor)
                        definitions.extend(page.tools)
                        cursor = page.nextCursor
                        if not cursor:
                            break
                        if cursor in cursors:
                            raise ValueError("MCP 工具分页游标重复")
                        cursors.add(cursor)
                    allowed = self.config.allowed_tools
                    if allowed is not None:
                        missing = set(allowed) - {tool.name for tool in definitions}
                        if missing:
                            names = ", ".join(sorted(missing))
                            raise ValueError(f"MCP allowed_tools 不存在: {names}")
                    self.tools = [
                        MCPTool(self, tool)
                        for tool in definitions
                        if allowed is None or tool.name in allowed
                    ]
                    names = [tool.name for tool in self.tools]
                    if len(set(names)) != len(names):
                        raise ValueError("MCP 服务返回了重复工具名称")
                    self._session = session
                    self._startup_complete = True
                if not ready.done():
                    ready.set_result(None)
                await self._stop.wait()
        except BaseException as exc:
            self._error = exc
            if not ready.done():
                ready.set_exception(
                    RuntimeError(f"MCP 服务 {self.config.name} 连接失败（{type(exc).__name__}）")
                )
            else:
                logger.error(f"MCP 服务 {self.config.name} 连接终止（{type(exc).__name__}）")
        finally:
            self._session = None

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        if self._session is None or self._stop.is_set():
            return f"错误: MCP 服务 {self.config.name} 未连接"
        try:
            async with asyncio.timeout(self.config.timeout):
                result = await self._session.call_tool(name, arguments=arguments)
            return format_result(result)
        except TimeoutError:
            return f"错误: MCP 服务 {self.config.name} 调用超时（{self.config.timeout:g} 秒）"
        except Exception as exc:
            # 服务响应或 HTTP 错误可能包含认证头，不直接返回原始异常。
            return f"错误: MCP 服务 {self.config.name} 调用失败（{type(exc).__name__}）"

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            await self._task
        if self._startup_complete and self._error is not None:
            raise RuntimeError(f"MCP 服务 {self.config.name} 连接异常或清理失败") from self._error


class MCPClientManager:
    """一个服务进程共享 MCP 连接，各 Agent 仍使用自己的工具注册中心。"""

    def __init__(self, configs: list[MCPServerConfig]):
        names = [config.name for config in configs]
        if len(set(names)) != len(names):
            raise ValueError("MCP 服务名称不能重复")
        self._configs = configs
        self._connections: list[MCPConnection] = []
        self._started = False
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        async with self._lock:
            if self._started:
                return
            try:
                for config in self._configs:
                    if not config.enabled:
                        continue
                    connection = MCPConnection(config)
                    self._connections.append(connection)
                    await connection.start()
                    count = len(connection.tools)
                    logger.info(f"MCP 服务 {config.name} 已连接，发现 {count} 个工具")
                self._started = True
            except BaseException:
                await self._close_connections()
                raise

    def attach_tools(self, registry: ToolRegistry) -> None:
        if not self._started:
            return
        tools = [tool for connection in self._connections for tool in connection.tools]
        for tool in tools:
            existing = registry.get(tool.name)
            if existing is not None and existing is not tool:
                raise ValueError(f"MCP 工具名称冲突: {tool.name}")
        for tool in tools:
            if registry.get(tool.name) is None:
                registry.add_tool(tool)

    async def _close_connections(self) -> None:
        results = await asyncio.gather(
            *(connection.stop() for connection in self._connections),
            return_exceptions=True,
        )
        self._connections.clear()
        self._started = False
        errors = [result for result in results if isinstance(result, BaseException)]
        if errors:
            raise RuntimeError("MCP 连接清理失败") from errors[0]

    async def stop(self) -> None:
        async with self._lock:
            await self._close_connections()
