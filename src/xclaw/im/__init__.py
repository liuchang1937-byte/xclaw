"""
IM 接入层 - 即时通讯平台适配器

职责：
- 统一的 IM 适配器接口
- 飞书适配器（Webhook + 事件订阅）
- QQ 适配器（WebSocket Bot 或 HTTP 桥接）
- 微信适配器（iLink 长轮询）
- 消息路由和格式化

设计理念：
- 适配器模式：统一接口，多种后端
- 消息标准化：不同平台的消息转为统一格式
"""

from xclaw.im.adapter import IMAdapter, IMMessage, IMReply
from xclaw.im.feishu import FeishuAdapter
from xclaw.im.qq import QQAdapter
from xclaw.im.qq_bridge import QQBridgeAdapter
from xclaw.im.weixin import WeixinAdapter

__all__ = [
    "IMAdapter",
    "IMMessage",
    "IMReply",
    "FeishuAdapter",
    "QQAdapter",
    "QQBridgeAdapter",
    "WeixinAdapter",
]
