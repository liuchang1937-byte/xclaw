"""
消息队列 - 解耦消息的生产和消费

支持：
- 优先级队列（高优先级消息优先处理）
- 延迟消息（指定时间后才可消费）
- 死信队列（处理失败的消息）

设计理念：为什么不用 Redis/Kafka？
- 在 Agent 场景中，消息量相对较小
- 内存队列足够快，且零依赖
- 保持框架的"极简"理念
- 需要持久化时可以替换为实现同样的接口
"""

from __future__ import annotations

import asyncio
import heapq
import time
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, AsyncIterator, Callable

from loguru import logger


class MessagePriority(IntEnum):
    """消息优先级"""
    LOW = 0
    NORMAL = 5
    HIGH = 10
    CRITICAL = 15


@dataclass(order=True)
class Message:
    """一条消息"""
    # 排序字段（由 priority 和 created_at 决定）
    _sort_key: tuple[int, float] = field(init=False, compare=False)

    id: str = field(compare=False)
    topic: str = field(compare=False)  # 消息主题（用于路由）
    content: Any = field(compare=False)  # 消息内容
    priority: MessagePriority = field(default=MessagePriority.NORMAL, compare=False)
    created_at: float = field(default_factory=time.time, compare=False)
    deliver_at: float = field(default=0, compare=False)  # 延迟投递时间
    retry_count: int = field(default=0, compare=False)  # 已重试次数
    max_retries: int = field(default=3, compare=False)  # 最大重试次数
    metadata: dict[str, Any] = field(default_factory=dict, compare=False)

    def __post_init__(self):
        # 优先级取反（heapq 是最小堆，优先级高的应该先出队）
        self._sort_key = (-int(self.priority), self.created_at)

    @property
    def is_ready(self) -> bool:
        """消息是否可以被投递"""
        return time.time() >= self.deliver_at

    @property
    def can_retry(self) -> bool:
        """是否还可以重试"""
        return self.retry_count < self.max_retries


class MessageQueue:
    """基于堆的优先级消息队列

    特点：
    - 线程安全（基于 asyncio）
    - 支持优先级
    - 支持延迟消息
    - 自动重试机制

    使用示例：
        queue = MessageQueue()
        await queue.publish(Message(
            id="msg_001",
            topic="task.request",
            content={"action": "search", "query": "AI news"},
            priority=MessagePriority.HIGH,
        ))
        msg = await queue.consume("task.request")
    """

    def __init__(self, max_size: int = 10000):
        self.max_size = max_size
        self._heap: list[Message] = []
        self._lock = asyncio.Lock()
        self._not_empty = asyncio.Event()
        self._dead_letter_queue: list[Message] = []  # 死信队列

        # 统计
        self.published_count = 0
        self.consumed_count = 0
        self.failed_count = 0

    async def publish(self, message: Message) -> None:
        """发布一条消息

        Args:
            message: 要发布的消息
        """
        async with self._lock:
            if len(self._heap) >= self.max_size:
                logger.warning(f"消息队列已满 ({self.max_size})，丢弃最旧的消息")
                heapq.heappop(self._heap)

            heapq.heappush(self._heap, message)
            self.published_count += 1
            self._not_empty.set()
            logger.debug(
                f"消息已入队: {message.id} topic={message.topic} "
                f"priority={message.priority.name}"
            )

    async def consume(self, topic: str, timeout: float = 30.0) -> Message | None:
        """消费一条指定主题的消息

        Args:
            topic: 消息主题
            timeout: 等待超时时间（秒）

        Returns:
            Message | None: 消息（超时返回 None）
        """
        deadline = time.time() + timeout

        while time.time() < deadline:
            async with self._lock:
                # 找到第一个就绪且匹配主题的消息
                for i, msg in enumerate(self._heap):
                    if msg.topic == topic and msg.is_ready:
                        # 从堆中移除该消息
                        self._heap[i] = self._heap[-1]
                        self._heap.pop()
                        heapq.heapify(self._heap)

                        if not self._heap:
                            self._not_empty.clear()

                        self.consumed_count += 1
                        logger.debug(f"消息已消费: {msg.id} topic={msg.topic}")
                        return msg

            # 没有就绪消息，等待
            remaining = deadline - time.time()
            if remaining > 0:
                try:
                    await asyncio.wait_for(
                        self._not_empty.wait(), timeout=min(remaining, 1.0)
                    )
                except asyncio.TimeoutError:
                    pass
            self._not_empty.clear()

        return None

    async def consume_batch(
        self, topic: str, batch_size: int = 10, timeout: float = 5.0
    ) -> list[Message]:
        """批量消费消息

        Args:
            topic: 消息主题
            batch_size: 批量大小
            timeout: 等待超时

        Returns:
            list[Message]: 消息列表
        """
        messages = []
        for _ in range(batch_size):
            msg = await self.consume(topic, timeout=timeout)
            if msg:
                messages.append(msg)
            else:
                break
        return messages

    async def nack(self, message: Message) -> None:
        """消息处理失败，重新入队或放入死信队列

        Args:
            message: 处理失败的消息
        """
        message.retry_count += 1
        self.failed_count += 1

        if message.can_retry:
            # 延迟重试（指数退避）
            delay = 2 ** message.retry_count
            message.deliver_at = time.time() + delay
            logger.warning(
                f"消息 {message.id} 将在 {delay}s 后重试 "
                f"({message.retry_count}/{message.max_retries})"
            )
            await self.publish(message)
        else:
            # 放入死信队列
            self._dead_letter_queue.append(message)
            logger.error(
                f"消息 {message.id} 已放入死信队列（重试次数已耗尽）"
            )

    async def ack(self, message: Message) -> None:
        """确认消息处理成功

        Args:
            message: 处理成功的消息
        """
        logger.debug(f"消息 {message.id} 已确认处理成功")

    def get_stats(self) -> dict[str, int]:
        """获取队列统计信息"""
        return {
            "queue_size": len(self._heap),
            "dead_letter_count": len(self._dead_letter_queue),
            "published": self.published_count,
            "consumed": self.consumed_count,
            "failed": self.failed_count,
        }

    def clear(self) -> None:
        """清空队列"""
        self._heap.clear()
        self._dead_letter_queue.clear()
        self._not_empty.clear()
