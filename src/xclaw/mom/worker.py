"""
Worker 管理 - 多任务执行引擎

职责：
- 管理 Worker 协程池
- 任务分发和结果收集
- 并发控制

对应 pi-mono 中的 Worker 管理和 PM2 进程管理概念。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Coroutine

from loguru import logger


class TaskStatus(str, Enum):
    """任务状态"""
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class WorkerTask:
    """一个工作任务"""
    id: str
    name: str = ""
    func: Callable[..., Coroutine[Any, Any, Any]] | None = field(
        default=None, repr=False
    )
    args: tuple = field(default_factory=tuple)
    kwargs: dict = field(default_factory=dict)
    status: TaskStatus = TaskStatus.PENDING
    result: Any = None
    error: str | None = None


class WorkerPool:
    """Worker 协程池

    管理一组协程 Worker，控制并发数量。

    使用示例：
        pool = WorkerPool(max_workers=4)

        task_id = pool.submit(
            "search_task",
            my_search_func,
            "AI news",
            max_results=5,
        )

        result = await pool.wait_for(task_id)
    """

    def __init__(self, max_workers: int = 4):
        self.max_workers = max_workers
        self._semaphore = asyncio.Semaphore(max_workers)
        self._tasks: dict[str, WorkerTask] = {}
        self._running_tasks: set[str] = set()
        self._completed_tasks: dict[str, Any] = {}
        self._task_counter = 0

    def submit(
        self,
        name: str,
        func: Callable[..., Coroutine[Any, Any, Any]],
        *args,
        **kwargs,
    ) -> str:
        """提交一个任务

        Args:
            name: 任务名称
            func: 异步任务函数
            *args: 位置参数
            **kwargs: 关键字参数

        Returns:
            str: 任务 ID
        """
        self._task_counter += 1
        task_id = f"task_{self._task_counter}"

        task = WorkerTask(
            id=task_id,
            name=name,
            func=func,
            args=args,
            kwargs=kwargs,
        )
        self._tasks[task_id] = task

        # 创建执行协程
        asyncio.create_task(self._run_task(task_id))

        logger.debug(f"任务已提交: {task_id} ({name})")
        return task_id

    async def _run_task(self, task_id: str) -> None:
        """执行任务"""
        task = self._tasks.get(task_id)
        if task is None or task.func is None:
            return

        # 获取信号量（控制并发）
        async with self._semaphore:
            task.status = TaskStatus.RUNNING
            self._running_tasks.add(task_id)
            logger.info(f"任务开始执行: {task_id} ({task.name})")

            try:
                result = await task.func(*task.args, **task.kwargs)
                task.status = TaskStatus.COMPLETED
                task.result = result
                self._completed_tasks[task_id] = result
                logger.info(f"任务完成: {task_id} ({task.name})")

            except asyncio.CancelledError:
                task.status = TaskStatus.CANCELLED
                logger.warning(f"任务已取消: {task_id} ({task.name})")

            except Exception as e:
                task.status = TaskStatus.FAILED
                task.error = str(e)
                logger.error(f"任务失败: {task_id} ({task.name}): {e}")

            finally:
                self._running_tasks.discard(task_id)

    async def wait_for(self, task_id: str, timeout: float = 60.0) -> Any:
        """等待任务完成并获取结果

        Args:
            task_id: 任务 ID
            timeout: 超时时间

        Returns:
            Any: 任务结果
        """
        task = self._tasks.get(task_id)
        if task is None:
            raise ValueError(f"任务不存在: {task_id}")

        if task.status == TaskStatus.COMPLETED:
            return task.result

        if task.status == TaskStatus.FAILED:
            raise RuntimeError(f"任务执行失败: {task.error}")

        # 等待任务完成
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            if task_id in self._completed_tasks:
                return self._completed_tasks[task_id]
            if task.status == TaskStatus.FAILED:
                raise RuntimeError(f"任务执行失败: {task.error}")
            if task.status == TaskStatus.CANCELLED:
                raise RuntimeError("任务已被取消")
            await asyncio.sleep(0.1)

        raise TimeoutError(f"任务 {task_id} 超时 ({timeout}s)")

    async def wait_all(self, timeout: float = 120.0) -> dict[str, Any]:
        """等待所有任务完成

        Args:
            timeout: 超时时间

        Returns:
            dict[str, Any]: 任务 ID → 结果
        """
        pending_ids = [
            tid for tid, t in self._tasks.items()
            if t.status in (TaskStatus.PENDING, TaskStatus.RUNNING)
        ]

        if not pending_ids:
            return dict(self._completed_tasks)

        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            all_done = all(
                self._tasks[tid].status in (
                    TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED
                )
                for tid in pending_ids
            )
            if all_done:
                break
            await asyncio.sleep(0.2)

        return dict(self._completed_tasks)

    def cancel(self, task_id: str) -> None:
        """取消任务

        Args:
            task_id: 任务 ID
        """
        task = self._tasks.get(task_id)
        if task and task.status == TaskStatus.RUNNING:
            task.status = TaskStatus.CANCELLED
            logger.info(f"取消任务: {task_id}")

    def get_stats(self) -> dict[str, int]:
        """获取 Worker 池统计信息"""
        return {
            "total": len(self._tasks),
            "running": len(self._running_tasks),
            "completed": len(self._completed_tasks),
            "pending": sum(
                1 for t in self._tasks.values()
                if t.status == TaskStatus.PENDING
            ),
            "failed": sum(
                1 for t in self._tasks.values()
                if t.status == TaskStatus.FAILED
            ),
        }
