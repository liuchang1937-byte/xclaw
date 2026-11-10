"""
PM2 进程管理模块 - Agent 的长期运行和进程守护

职责：
- 守护进程：保持 Agent 在后台持续运行
- 进程监控：检测异常并自动重启
- 日志管理：日志轮转和持久化

设计理念：
- 参考 PM2 的进程管理思想，用纯 Python 实现
- 支持 Windows / Linux / macOS
- 轻量级，零外部依赖（不需要安装 Node.js PM2）
"""

from xclaw.pm2.daemon import Daemon, run_as_daemon
from xclaw.pm2.monitor import Monitor, HealthCheck

__all__ = [
    "Daemon",
    "run_as_daemon",
    "Monitor",
    "HealthCheck",
]
