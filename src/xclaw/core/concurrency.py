"""只读操作共享执行权，其他操作独占；取消时等待后台读取真正退出。"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager


async def run_blocking(func, *args, **kwargs):
    task = asyncio.create_task(asyncio.to_thread(func, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        # to_thread 的线程不会随协程取消，不能提前释放执行锁。
        try:
            await task
        except Exception:
            pass
        raise


class ReadWriteGate:
    """写者优先，避免持续到来的读取让独占操作一直等待。"""

    def __init__(self):
        self._condition = asyncio.Condition()
        self._readers = 0
        self._writer = False
        self._waiting_writers = 0

    @asynccontextmanager
    async def hold(self, *, shared: bool):
        async with self._condition:
            if shared:
                await self._condition.wait_for(
                    lambda: not self._writer and not self._waiting_writers
                )
                self._readers += 1
            else:
                self._waiting_writers += 1
                try:
                    await self._condition.wait_for(
                        lambda: not self._writer and not self._readers
                    )
                    self._writer = True
                finally:
                    self._waiting_writers -= 1
                    self._condition.notify_all()
        try:
            yield
        finally:
            async with self._condition:
                if shared:
                    self._readers -= 1
                else:
                    self._writer = False
                self._condition.notify_all()
