"""
服务模块 - 主入口和配置管理

职责：
- 配置管理（YAML 文件 + 环境变量）
- 主服务编排
- HTTP Server（用于接收 IM 回调）
"""

from xclaw.server.config import Config, load_config
from xclaw.server.main import main

__all__ = ["Config", "load_config", "main"]
