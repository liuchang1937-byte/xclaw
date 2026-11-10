"""
消息中间件模块 - 对应 pi-mono 中的 pi-mom

职责：
- 消息队列：解耦消息的生产和消费
- 事件总线：模块间的松耦合通信
- Worker 管理：多进程/多线程任务执行

设计理念：
- 异步优先：所有操作都是非阻塞的
- 类型安全：消息格式强类型约束
- 可观测：每条消息的处理都可追踪
"""

from xclaw.mom.queue import MessageQueue, Message, MessagePriority
from xclaw.mom.bus import EventBus, Event, EventHandler
from xclaw.mom.worker import WorkerPool, WorkerTask

__all__ = [
    "MessageQueue",
    "Message",
    "MessagePriority",
    "EventBus",
    "Event",
    "EventHandler",
    "WorkerPool",
    "WorkerTask",
]
