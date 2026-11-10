"""
进程监控 - 健康检查和状态监控

职责：
- 定时健康检查
- 资源使用监控
- 告警通知

对应 PM2 中的监控和状态查看功能。
"""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass, field

from loguru import logger


@dataclass
class HealthCheck:
    """健康检查结果"""
    healthy: bool = True
    message: str = "OK"
    timestamp: float = field(default_factory=time.time)

    # 资源使用
    cpu_percent: float = 0.0
    memory_mb: float = 0.0
    uptime_seconds: float = 0.0

    # Agent 状态
    agent_running: bool = True
    tool_count: int = 0
    pending_tasks: int = 0


class Monitor:
    """进程监控器

    定期检查 Agent 服务的健康状态。

    使用示例：
        monitor = Monitor(agent, check_interval=30)
        monitor.on_unhealthy(lambda: send_alert("Agent is down!"))
        await monitor.start()
    """

    def __init__(
        self,
        agent=None,
        *,
        check_interval: float = 30.0,
        memory_threshold_mb: float = 500.0,
    ):
        """
        初始化监控器

        Args:
            agent: 要监控的 Agent 实例
            check_interval: 检查间隔（秒）
            memory_threshold_mb: 内存告警阈值（MB）
        """
        self.agent = agent
        self.check_interval = check_interval
        self.memory_threshold_mb = memory_threshold_mb

        self._start_time = time.time()
        self._running = False
        self._task: asyncio.Task | None = None
        self._unhealthy_callbacks: list[callable] = []

        # 健康检查历史
        self._checks: list[HealthCheck] = []

    @property
    def uptime_seconds(self) -> float:
        """运行时长（秒）"""
        return time.time() - self._start_time

    async def start(self) -> None:
        """启动监控"""
        self._running = True
        self._start_time = time.time()
        self._task = asyncio.create_task(self._monitor_loop())
        logger.info(f"监控器已启动 (间隔: {self.check_interval}s)")

    async def stop(self) -> None:
        """停止监控"""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("监控器已停止")

    async def _monitor_loop(self) -> None:
        """监控循环"""
        while self._running:
            try:
                check = await self.run_health_check()
                self._checks.append(check)

                if not check.healthy:
                    logger.error(f"健康检查失败: {check.message}")
                    await self._trigger_unhealthy(check)

                # 只在 DEBUG 级别记录正常状态
                logger.debug(
                    f"健康检查: healthy={check.healthy}, "
                    f"memory={check.memory_mb:.1f}MB, "
                    f"cpu={check.cpu_percent:.1f}%"
                )

            except Exception as e:
                logger.error(f"监控异常: {e}")

            await asyncio.sleep(self.check_interval)

    async def run_health_check(self) -> HealthCheck:
        """执行一次健康检查

        Returns:
            HealthCheck: 检查结果
        """
        check = HealthCheck(uptime_seconds=self.uptime_seconds)

        try:
            # 检查 Agent 状态
            if self.agent:
                check.agent_running = self.agent._is_running
                check.tool_count = len(self.agent.tool_registry)

            # 获取内存使用
            try:
                import psutil
                process = psutil.Process(os.getpid())
                mem_info = process.memory_info()
                check.memory_mb = mem_info.rss / (1024 * 1024)
                check.cpu_percent = process.cpu_percent(interval=0.1)

                # 内存告警
                if check.memory_mb > self.memory_threshold_mb:
                    check.healthy = False
                    check.message = (
                        f"内存使用过高: {check.memory_mb:.1f}MB "
                        f"(阈值: {self.memory_threshold_mb}MB)"
                    )
            except ImportError:
                # psutil 未安装，跳过资源检查
                pass

        except Exception as e:
            check.healthy = False
            check.message = f"健康检查异常: {e}"

        return check

    def on_unhealthy(self, callback: callable) -> None:
        """注册不健康时的回调

        Args:
            callback: 回调函数（接收 HealthCheck 参数）
        """
        self._unhealthy_callbacks.append(callback)

    async def _trigger_unhealthy(self, check: HealthCheck) -> None:
        """触发不健康回调"""
        for callback in self._unhealthy_callbacks:
            try:
                result = callback(check)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as e:
                logger.error(f"不健康回调执行失败: {e}")

    def get_report(self) -> str:
        """生成监控报告

        Returns:
            str: 格式化的监控报告
        """
        lines = [
            "=" * 40,
            "  XClaw 监控报告",
            "=" * 40,
            f"  运行时间: {self._format_uptime(self.uptime_seconds)}",
            f"  检查次数: {len(self._checks)}",
        ]

        if self._checks:
            latest = self._checks[-1]
            lines.extend([
                f"  最近检查: {'✅ 正常' if latest.healthy else '❌ 异常'}",
                f"  内存使用: {latest.memory_mb:.1f} MB",
                f"  CPU 使用: {latest.cpu_percent:.1f}%",
                f"  工具数量: {latest.tool_count}",
            ])

        # 最近的不健康记录
        unhealthy = [c for c in self._checks[-10:] if not c.healthy]
        if unhealthy:
            lines.append(f"\n  最近异常 ({len(unhealthy)}):")
            for c in unhealthy:
                lines.append(f"    - {c.message}")

        lines.append("=" * 40)
        return "\n".join(lines)

    @staticmethod
    def _format_uptime(seconds: float) -> str:
        """格式化运行时间"""
        hours, remainder = divmod(int(seconds), 3600)
        minutes, seconds = divmod(remainder, 60)
        parts = []
        if hours:
            parts.append(f"{hours}h")
        if minutes:
            parts.append(f"{minutes}m")
        parts.append(f"{seconds}s")
        return " ".join(parts)
