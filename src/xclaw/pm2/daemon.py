"""
守护进程 - 保持 Agent 服务在后台持续运行

参考 PM2 的设计思想：
- 进程守护：主进程监控子进程
- 自动重启：子进程异常退出时自动拉起
- 优雅关闭：收到信号时优雅关闭子进程
- 日志管理：捕获和轮转子进程的输出

注意：
- Windows 不支持 fork，使用 spawn 方式
- 设计为跨平台（Windows / Linux / macOS）
"""

from __future__ import annotations

import asyncio
import multiprocessing
import os
import signal
import sys
import time
from pathlib import Path

from loguru import logger


class Daemon:
    """守护进程管理器

    管理一个子进程（Agent 服务），确保它在后台持续运行。

    使用示例：
        daemon = Daemon(
            target=run_agent_server,
            name="xclaw-agent",
            restart_delay=3,
        )
        daemon.start()
    """

    def __init__(
        self,
        target: callable,
        name: str = "xclaw-worker",
        *,
        restart_delay: float = 3.0,
        max_restarts: int = 10,
        restart_window: float = 60.0,
        log_dir: str = "logs",
    ):
        """
        初始化守护进程

        Args:
            target: 要守护的运行函数
            name: 进程名称
            restart_delay: 重启延迟（秒）
            max_restarts: 时间窗口内最大重启次数
            restart_window: 重启计数窗口（秒）
            log_dir: 日志目录
        """
        self.target = target
        self.name = name
        self.restart_delay = restart_delay
        self.max_restarts = max_restarts
        self.restart_window = restart_window
        self.log_dir = Path(log_dir)

        # 运行时状态
        self._process: multiprocessing.Process | None = None
        self._running = False
        self._restart_times: list[float] = []

    def start(self) -> None:
        """启动守护进程（阻塞）"""
        self._running = True
        logger.info(f"守护进程启动: {self.name}")

        # 确保日志目录存在
        self.log_dir.mkdir(parents=True, exist_ok=True)

        while self._running:
            try:
                # 启动子进程
                self._process = multiprocessing.Process(
                    target=self._run_target,
                    name=self.name,
                    daemon=True,
                )
                self._process.start()
                logger.info(
                    f"子进程已启动: {self.name} (PID: {self._process.pid})"
                )

                # 等待子进程结束
                self._process.join()

                # 子进程退出后的处理
                exit_code = self._process.exitcode
                logger.warning(
                    f"子进程已退出: {self.name} "
                    f"(PID: {self._process.pid}, exit={exit_code})"
                )

                if not self._running:
                    break

                # 检查是否需要重启
                if self._should_restart():
                    logger.info(
                        f"将在 {self.restart_delay}s 后重启子进程..."
                    )
                    time.sleep(self.restart_delay)
                else:
                    logger.error(
                        f"子进程 {self.name} 在 {self.restart_window}s 内"
                        f"重启了 {self.max_restarts} 次，停止守护"
                    )
                    break

            except KeyboardInterrupt:
                logger.info("收到中断信号，停止守护进程")
                break

            except Exception as e:
                logger.error(f"守护进程异常: {e}")
                if self._running:
                    time.sleep(self.restart_delay)

        # 清理
        self._running = False
        logger.info(f"守护进程已停止: {self.name}")

    def stop(self) -> None:
        """停止守护进程"""
        self._running = False

        if self._process and self._process.is_alive():
            logger.info(f"正在终止子进程: {self._process.pid}")
            self._process.terminate()
            self._process.join(timeout=5)

            if self._process.is_alive():
                logger.warning(f"强制杀死子进程: {self._process.pid}")
                self._process.kill()

    def _run_target(self) -> None:
        """在子进程中运行目标函数"""
        try:
            # 配置子进程日志
            logger.add(
                self.log_dir / f"{self.name}.log",
                rotation="10 MB",
                retention="7 days",
                level="DEBUG",
            )

            # 运行目标函数
            if asyncio.iscoroutinefunction(self.target):
                asyncio.run(self.target())
            else:
                self.target()

        except KeyboardInterrupt:
            pass
        except Exception as e:
            logger.error(f"子进程运行异常: {e}")
            sys.exit(1)

    def _should_restart(self) -> bool:
        """判断是否应该重启子进程

        防止短时间内无限重启（类似 PM2 的 restart 策略）。

        Returns:
            bool: 是否应该重启
        """
        now = time.time()

        # 清理旧的记录
        self._restart_times = [
            t for t in self._restart_times
            if now - t < self.restart_window
        ]

        self._restart_times.append(now)

        # 检查是否超过最大重启次数
        if len(self._restart_times) > self.max_restarts:
            return False

        return True


def run_as_daemon(target: callable, name: str = "xclaw") -> None:
    """便捷函数：以守护进程模式运行

    Args:
        target: 要守护的函数
        name: 进程名称
    """
    daemon = Daemon(target=target, name=name)
    daemon.start()
