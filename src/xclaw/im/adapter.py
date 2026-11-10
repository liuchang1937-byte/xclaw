"""
IM 适配器接口 - 统一的即时通讯接入抽象

定义所有 IM 适配器必须实现的接口。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Coroutine

from loguru import logger


@dataclass
class IMMessage:
    """标准化的 IM 消息格式

    无论来自飞书、QQ 还是其他平台，
    都会被转换为这个统一格式。
    """
    # 消息元信息
    platform: str  # 来源适配器，如 "feishu"、"qq"、"qq_bridge"、"weixin"
    message_id: str  # 平台上的消息 ID
    chat_id: str  # 会话 ID（群聊或私聊）
    chat_type: str  # 会话类型: "group" / "private"

    # 发送者信息
    sender_id: str  # 发送者 ID
    sender_name: str = ""  # 发送者名称

    # 消息内容
    content: str = ""  # 纯文本内容
    raw_content: Any = None  # 原始消息内容（平台特定格式）

    # 引用/回复
    reply_to: str | None = None  # 引用的消息 ID

    # 附件
    attachments: list[dict] = field(default_factory=list)

    # 时间戳
    timestamp: float = 0.0


@dataclass
class IMReply:
    """最终回复及显式交付的本轮附件。"""

    text: str = ""
    files: list[Path] = field(default_factory=list)


# 消息处理回调类型
MessageHandler = Callable[[IMMessage], Coroutine[Any, Any, str | IMReply | None]]


class IMAdapter(ABC):
    """IM 适配器抽象基类

    所有 IM 平台适配器都需要实现此接口。
    """

    supports_files = False

    def __init__(self, name: str):
        self.name = name
        self._handlers: list[MessageHandler] = []

    @abstractmethod
    async def start(self) -> None:
        """启动适配器（开始监听消息）"""
        ...

    @abstractmethod
    async def stop(self) -> None:
        """停止适配器"""
        ...

    @abstractmethod
    async def send_message(
        self,
        chat_id: str,
        content: str,
        *,
        reply_to: str | None = None,
        chat_type: str = "group",
    ) -> bool:
        """发送消息到指定会话

        Args:
            chat_id: 目标会话 ID
            content: 消息内容
            reply_to: 要回复的消息 ID
            chat_type: 会话类型

        Returns:
            bool: 发送是否成功
        """
        ...

    async def send_file(
        self, chat_id: str, path: Path, *, reply_to: str | None = None,
        chat_type: str = "group",
    ) -> bool:
        return False

    def file_failure_reason(self) -> str:
        return ""

    def on_message(self, handler: MessageHandler) -> MessageHandler:
        """注册消息处理器（装饰器）

        使用方式：
            @adapter.on_message
            async def handle(msg: IMMessage) -> str | None:
                return "收到你的消息了！"

        Args:
            handler: 消息处理函数，接收 IMMessage，返回可选的回复内容

        Returns:
            原始的 handler 函数
        """
        self._handlers.append(handler)
        logger.info(f"[{self.name}] 注册消息处理器: {handler.__name__}")
        return handler

    async def _dispatch(self, message: IMMessage) -> None:
        """分发消息到所有处理器

        Args:
            message: 标准化的消息
        """
        logger.info(
            f"[{self.name}] 分发消息: chat_id={message.chat_id[:20]}..., "
            f"chat_type={message.chat_type}, "
            f"content={message.content[:50]}..."
        )
        for handler in self._handlers:
            try:
                reply = await handler(message)
                if isinstance(reply, IMReply):
                    failed = []
                    for path in reply.files:
                        if not await self.send_file(
                            message.chat_id, path, reply_to=message.message_id,
                            chat_type=message.chat_type,
                        ):
                            detail = self.file_failure_reason()
                            failed.append(f"{path.name}（{detail}）" if detail else path.name)
                    reply = (
                        f"文件已生成，但发送失败：{', '.join(failed)}。"
                        "请根据错误提示处理后重试。"
                        if failed else reply.text
                    )
                if reply:
                    logger.info(
                        f"[{self.name}] Agent 回复 ({len(reply)} 字符), "
                        f"正在发送到 chat_id={message.chat_id[:20]}..."
                    )
                    success = await self.send_message(
                        chat_id=message.chat_id,
                        content=reply,
                        reply_to=message.message_id,
                        chat_type=message.chat_type,
                    )
                    if success:
                        logger.info(f"[{self.name}] 回复已发送")
                    else:
                        logger.error(f"[{self.name}] 回复发送失败！")
                else:
                    logger.warning(f"[{self.name}] Agent 未返回回复内容")
            except Exception as e:
                logger.error(
                    f"[{self.name}] 消息处理失败 [{handler.__name__}]: {e}"
                )

    @staticmethod
    def format_code_block(code: str, language: str = "") -> str:
        """格式化为代码块（不同平台格式不同，此处提供默认实现）

        Args:
            code: 代码内容
            language: 编程语言

        Returns:
            str: 格式化后的代码块
        """
        return f"```{language}\n{code}\n```"

    @staticmethod
    def truncate_message(content: str, max_length: int = 2000) -> str:
        """截断过长消息

        Args:
            content: 消息内容
            max_length: 最大长度

        Returns:
            str: 截断后的消息
        """
        if len(content) <= max_length:
            return content

        truncation_msg = f"\n\n... (内容过长，已截断，原 {len(content)} 字符)"
        return content[:max_length - len(truncation_msg)] + truncation_msg
