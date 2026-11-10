"""
事件总线 - 模块间的松耦合通信

设计理念：
- 发布/订阅模式
- 同步和异步处理器支持
- 类型安全的事件定义

对应 pi-mono 中模块间的事件通信机制。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine

from loguru import logger


@dataclass
class Event:
    """事件 - 总线上传递的消息单元"""
    type: str  # 事件类型（如 "agent.started", "tool.executed"）
    source: str = ""  # 事件来源
    data: Any = None  # 事件数据
    timestamp: float = field(default_factory=__import__("time").time)


# 事件处理器类型：同步函数或异步函数
EventHandler = Callable[[Event], None] | Callable[[Event], Coroutine[Any, Any, None]]


class EventBus:
    """事件总线

    特点：
    - 支持按事件类型订阅
    - 支持通配符订阅（如 "agent.*"）
    - 异步分发（不阻塞发布者）
    - 错误隔离（一个处理器的失败不影响其他处理器）

    使用示例：
        bus = EventBus()

        @bus.on("agent.started")
        async def on_agent_started(event: Event):
            print(f"Agent 已启动: {event.data}")

        await bus.emit(Event(type="agent.started", data={"agent": "XClaw"}))
    """

    def __init__(self):
        # 事件类型 → 处理器列表
        self._handlers: dict[str, list[EventHandler]] = {}
        # 通配符处理器
        self._wildcard_handlers: list[tuple[str, EventHandler]] = []

    def on(self, event_type: str):
        """装饰器：注册事件处理器

        使用方式：
            @bus.on("tool.executed")
            def handle_tool_executed(event: Event):
                print(f"工具已执行: {event.data}")

        支持通配符：
            @bus.on("agent.*")
            def handle_all_agent_events(event: Event):
                print(f"Agent 事件: {event.type}")

        Args:
            event_type: 事件类型（支持 * 通配符）
        """

        def decorator(handler: EventHandler):
            self.register(event_type, handler)
            return handler

        return decorator

    def register(self, event_type: str, handler: EventHandler) -> None:
        """注册事件处理器

        Args:
            event_type: 事件类型
            handler: 处理器函数
        """
        if "*" in event_type:
            self._wildcard_handlers.append((event_type, handler))
        else:
            if event_type not in self._handlers:
                self._handlers[event_type] = []
            self._handlers[event_type].append(handler)
        logger.debug(f"注册事件处理器: {event_type}")

    def unregister(self, event_type: str, handler: EventHandler) -> None:
        """取消注册事件处理器

        Args:
            event_type: 事件类型
            handler: 处理器函数
        """
        if event_type in self._handlers:
            self._handlers[event_type] = [
                h for h in self._handlers[event_type] if h != handler
            ]

    async def emit(self, event: Event) -> None:
        """发布事件（异步分发）

        Args:
            event: 要发布的事件
        """
        handlers: list[EventHandler] = []

        # 精确匹配
        if event.type in self._handlers:
            handlers.extend(self._handlers[event.type])

        # 通配符匹配
        import fnmatch
        for pattern, handler in self._wildcard_handlers:
            if fnmatch.fnmatch(event.type, pattern):
                handlers.append(handler)

        if not handlers:
            logger.debug(f"事件 {event.type} 没有处理器")
            return

        logger.debug(
            f"分发事件: {event.type} → {len(handlers)} 个处理器"
        )

        # 并发执行所有处理器
        async def safe_call(handler: EventHandler):
            try:
                result = handler(event)
                # 支持异步处理器
                if asyncio.iscoroutine(result):
                    await result
            except Exception as e:
                logger.error(f"事件处理器执行失败 [{event.type}]: {e}")

        await asyncio.gather(*[safe_call(h) for h in handlers])

    def emit_sync(self, event: Event) -> None:
        """同步发布事件（用于非异步上下文）"""
        try:
            loop = asyncio.get_running_loop()
            # 在运行的事件循环中，创建任务
            loop.create_task(self.emit(event))
        except RuntimeError:
            # 没有运行的事件循环，同步执行
            asyncio.run(self.emit(event))
