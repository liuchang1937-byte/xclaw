"""
QQ 适配器 - 接入 QQ Bot

通过 QQ 开放平台的 WebSocket 接口收发消息。

前置条件：
1. 在 QQ 开放平台 (https://q.qq.com) 注册并创建 Bot
2. 获取 **BotAppID** 和 **AppSecret**（也叫 ClientSecret）
   - 注意：QQ 平台没有 BotToken 这个概念
   - AppSecret 在"开发设置"页面的"机器人密钥"处生成
3. 配置 Bot 的权限（消息收发）

鉴权流程：
  AppID + AppSecret → POST /app/getAppAccessToken → access_token
  所有 API 请求头: Authorization: QQBot {access_token}

注意：
- QQ Bot 需要 WebSocket 长连接（不需要公网 IP！）
- 消息格式与飞书不同，需要适配
- 群聊和私聊的消息路由规则不同
- access_token 有效期 2 小时，会自动刷新

使用示例：
    adapter = QQAdapter(
        bot_app_id="1020xxxxx",
        bot_app_secret="xxxxxxxxxxxx",
    )
    adapter.on_message(handle_message)
    await adapter.start()
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import httpx
import websockets
from loguru import logger

from xclaw.im.adapter import IMAdapter, IMMessage


class QQAdapter(IMAdapter):
    """QQ Bot 适配器

    通过 QQ 开放平台 WebSocket 接口收发消息。

    鉴权方式：
    - 使用 AppID + AppSecret 获取 access_token
    - API 请求头: Authorization: QQBot {access_token}
    - token 过期前自动刷新
    """

    # QQ Bot API 基地址
    API_BASE = "https://api.sgroup.qq.com"

    def __init__(
        self,
        *,
        bot_app_id: str = "",
        bot_app_secret: str = "",
        sandbox: bool = False,
    ):
        """
        初始化 QQ 适配器

        Args:
            bot_app_id: QQ Bot 的 AppID（在 QQ 开放平台"开发设置"页获取）
            bot_app_secret: QQ Bot 的 AppSecret / ClientSecret（在"机器人密钥"处生成）
            sandbox: 是否使用沙箱环境（审核通过前可用沙箱测试）
        """
        super().__init__(name="qq")

        self.bot_app_id = bot_app_id
        self.bot_app_secret = bot_app_secret
        self.sandbox = sandbox

        # Access Token 管理
        self._access_token: str = ""
        self._token_expires_at: float = 0

        # WebSocket 连接
        self._ws: Any = None
        self._ws_task: asyncio.Task | None = None
        self._heartbeat_task: asyncio.Task | None = None
        self._session_id: str = ""
        self._last_seq: int = 0

        # HTTP 客户端（延迟初始化，因为需要 access_token）
        self._http_client: httpx.AsyncClient | None = None

    # ─── 生命周期 ──────────────────────────────────────

    async def start(self) -> None:
        """启动 QQ 适配器

        启动流程：
        1. 用 AppID + AppSecret 换取 access_token
        2. 用 access_token 获取 WebSocket 网关地址
        3. 建立 WebSocket 长连接
        4. 启动心跳保活
        5. 开始监听消息
        """
        # 第一步：获取 access_token
        if not await self._refresh_access_token():
            logger.error("[QQ] 无法获取 access_token，请检查 AppID 和 AppSecret")
            return

        # 创建 HTTP 客户端（带鉴权头）
        self._http_client = httpx.AsyncClient(
            timeout=30.0,
            headers={
                "Authorization": f"QQBot {self._access_token}",
                "Content-Type": "application/json",
            },
        )

        # 第二步：获取 WebSocket 地址
        ws_url = await self._get_ws_url()
        if not ws_url:
            logger.error("[QQ] 无法获取 WebSocket 地址")
            return

        # 第三步：启动 WebSocket 连接
        self._ws_task = asyncio.create_task(self._ws_loop(ws_url))
        logger.info(f"[QQ] 适配器已启动 (Bot: {self.bot_app_id})")

    async def stop(self) -> None:
        """停止 QQ 适配器"""
        # 停止心跳
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            self._heartbeat_task = None

        # 关闭 WebSocket
        if self._ws:
            await self._ws.close()
            self._ws = None

        # 取消 WebSocket 循环
        if self._ws_task:
            self._ws_task.cancel()
            self._ws_task = None

        # 关闭 HTTP 客户端
        if self._http_client:
            await self._http_client.aclose()
            self._http_client = None

        logger.info("[QQ] 适配器已停止")

    # ─── 鉴权 ──────────────────────────────────────────

    async def _refresh_access_token(self) -> bool:
        """用 AppID + AppSecret 换取 access_token

        QQ Bot 的鉴权接口：
            POST https://bots.qq.com/app/getAppAccessToken
            Body: {"appId": "xxx", "clientSecret": "xxx"}

        或者使用 OAuth2 方式：
            POST https://bots.qq.com/oauth2/token
            Body: grant_type=client_credentials&client_id=xxx&client_secret=xxx

        返回：
            {"access_token": "xxx", "expires_in": 7200}

        Returns:
            bool: 是否成功
        """
        if not self.bot_app_id or not self.bot_app_secret:
            logger.error("[QQ] AppID 或 AppSecret 未配置")
            return False

        try:
            # 使用独立的临时客户端（此时还没有正式的 _http_client）
            async with httpx.AsyncClient(timeout=30.0) as client:
                # 方式一：QQ Bot 专用鉴权接口
                response = await client.post(
                    "https://bots.qq.com/app/getAppAccessToken",
                    json={
                        "appId": self.bot_app_id,
                        "clientSecret": self.bot_app_secret,
                    },
                )

                if response.status_code != 200:
                    logger.error(
                        f"[QQ] 获取 access_token HTTP 错误: {response.status_code}"
                    )
                    return False

                data = response.json()

                # 检查错误码
                if data.get("code") and data.get("code") != 0:
                    # 方式一失败，尝试 OAuth2 方式
                    logger.debug(
                        f"[QQ] 方式一获取 token 失败 (code={data.get('code')})，"
                        f"尝试 OAuth2 方式..."
                    )

                    response2 = await client.post(
                        "https://bots.qq.com/oauth2/token",
                        data={
                            "grant_type": "client_credentials",
                            "client_id": self.bot_app_id,
                            "client_secret": self.bot_app_secret,
                        },
                    )
                    if response2.status_code == 200:
                        data = response2.json()
                    else:
                        logger.error(
                            f"[QQ] OAuth2 获取 token 也失败: {response2.status_code}"
                        )
                        return False

                self._access_token = data.get("access_token", "")
                expires_in = data.get("expires_in", 7200)

                if not self._access_token:
                    logger.error(
                        f"[QQ] 响应中没有 access_token: {json.dumps(data, ensure_ascii=False)}"
                    )
                    return False

                # 提前 5 分钟过期，确保不会用到过期的 token
                self._token_expires_at = time.time() + expires_in - 300

                logger.info(
                    f"[QQ] Access Token 已获取 (有效期 {expires_in}s, "
                    f"{self._access_token[:8]}...)"
                )
                return True

        except Exception as e:
            logger.error(f"[QQ] 获取 access_token 异常: {e}")
            return False

    def _ensure_token_valid(self) -> None:
        """检查 token 是否有效，过期则重新创建 HTTP 客户端"""
        if time.time() < self._token_expires_at:
            return  # token 还有效

        # Token 即将过期，需要在下次请求前刷新
        logger.warning("[QQ] Access Token 即将过期，需要刷新")
        # 注意：由于这是同步检查，实际刷新在下次 start() 或手动调用时完成
        # 更好的做法是启动一个定时刷新任务

    # ─── 消息发送 ──────────────────────────────────────

    async def send_message(
        self,
        chat_id: str,
        content: str,
        *,
        reply_to: str | None = None,
        chat_type: str = "group",
    ) -> bool:
        """发送消息到 QQ

        Args:
            chat_id: 群 ID（group_id）或用户 openid
            content: 消息内容（支持 Markdown 子集，QQ 可渲染部分 Markdown）
            reply_to: 要回复的消息 ID
            chat_type: "group"（群聊）或 "private"（私聊）

        Returns:
            bool: 发送是否成功
        """
        if not self._http_client:
            logger.error("[QQ] HTTP 客户端未初始化")
            return False

        # 确保 token 有效
        self._ensure_token_valid()

        try:
            # 构建消息体
            msg_body = {
                "content": self.truncate_message(content),
            }

            if reply_to:
                # 引用回复
                msg_body["msg_id"] = reply_to
                msg_body["message_reference"] = {
                    "message_id": reply_to,
                }

            # 确定 API 路径
            if chat_type == "group":
                url = f"{self.API_BASE}/v2/groups/{chat_id}/messages"
            else:
                # 私聊：先创建私信会话，再发送
                url = f"{self.API_BASE}/v2/users/{chat_id}/messages"

            response = await self._http_client.post(url, json=msg_body)

            if response.status_code == 200:
                result = response.json()
                if result.get("code") == 0:
                    logger.debug("[QQ] 消息发送成功")
                    return True
                elif result.get("code") == 401:
                    # Token 过期，尝试刷新
                    logger.warning("[QQ] Token 过期，尝试刷新...")
                    if await self._refresh_access_token():
                        # 更新 HTTP 客户端的鉴权头
                        self._http_client.headers["Authorization"] = (
                            f"QQBot {self._access_token}"
                        )
                        # 重试一次
                        response2 = await self._http_client.post(url, json=msg_body)
                        if response2.status_code == 200:
                            result2 = response2.json()
                            if result2.get("code") == 0:
                                logger.debug("[QQ] 刷新 token 后消息发送成功")
                                return True
                    return False
                else:
                    logger.error(f"[QQ] API 发送失败 (code={result.get('code')}): {result}")
                    return False
            elif response.status_code == 401:
                # Unauthorized
                logger.warning("[QQ] 401 Unauthorized，尝试刷新 token 后重试...")
                if await self._refresh_access_token():
                    self._http_client.headers["Authorization"] = (
                        f"QQBot {self._access_token}"
                    )
                    response2 = await self._http_client.post(url, json=msg_body)
                    if response2.status_code == 200:
                        result2 = response2.json()
                        if result2.get("code") == 0:
                            logger.debug("[QQ] 刷新 token 后消息发送成功")
                            return True
                return False
            else:
                logger.error(f"[QQ] HTTP 错误: {response.status_code}")
                return False

        except Exception as e:
            logger.error(f"[QQ] 发送异常: {e}")
            return False

    # ─── WebSocket 管理 ────────────────────────────────

    async def _get_ws_url(self) -> str | None:
        """获取 WebSocket 网关地址

        QQ Bot 获取网关接口：
            GET https://api.sgroup.qq.com/gateway
            Header: Authorization: QQBot {access_token}

        返回格式：
            {"url": "wss://api.sgroup.qq.com/websocket"}

        Returns:
            str | None: WebSocket 地址，失败返回 None
        """
        if not self._http_client:
            return None

        try:
            response = await self._http_client.get(
                f"{self.API_BASE}/gateway"
            )

            if response.status_code == 200:
                data = response.json()
                url = data.get("url", "")
                if url:
                    logger.info(f"[QQ] WebSocket 网关地址: {url}")
                    return url
                else:
                    logger.error(f"[QQ] 网关响应中没有 url 字段: {data}")
            elif response.status_code == 401:
                logger.error("[QQ] 获取网关地址 401，请检查 AppID 和 AppSecret")
            else:
                logger.error(f"[QQ] 获取网关地址失败: HTTP {response.status_code}")

            return None

        except Exception as e:
            logger.error(f"[QQ] 获取网关地址异常: {e}")
            return None

    async def _ws_loop(self, ws_url: str) -> None:
        """WebSocket 主循环

        处理连接、断线重连、消息接收。
        重连策略：指数退避，最大 60 秒。

        Args:
            ws_url: WebSocket 网关地址
        """
        retry_count = 0
        max_retry_delay = 60

        while True:
            try:
                logger.info("[QQ] 正在连接 WebSocket...")
                async with websockets.connect(
                    ws_url,
                    ping_interval=20,
                    ping_timeout=10,
                ) as ws:
                    self._ws = ws
                    retry_count = 0  # 连接成功，重置重试计数

                    logger.info("[QQ] WebSocket 连接成功")

                    # 启动心跳协程
                    self._heartbeat_task = asyncio.create_task(
                        self._heartbeat_loop()
                    )

                    # 接收消息循环
                    async for raw_message in ws:
                        await self._handle_ws_message(raw_message)

            except asyncio.CancelledError:
                logger.info("[QQ] WebSocket 循环被取消")
                break

            except websockets.ConnectionClosed as e:
                logger.warning(f"[QQ] WebSocket 连接关闭: code={e.code}")

            except Exception as e:
                logger.error(f"[QQ] WebSocket 异常: {type(e).__name__}: {e}")

            # 清理心跳
            if self._heartbeat_task:
                self._heartbeat_task.cancel()
                self._heartbeat_task = None

            self._ws = None

            # 重连等待（指数退避）
            retry_count += 1
            delay = min(2 ** retry_count, max_retry_delay)
            logger.info(f"[QQ] {delay}s 后将重连 (第 {retry_count} 次)...")
            await asyncio.sleep(delay)

    async def _heartbeat_loop(self) -> None:
        """心跳循环

        QQ Bot WebSocket 协议要求客户端定期发送心跳包（op=1）。
        发送间隔由服务端在 Hello (op=10) 消息中指定，
        默认使用 30 秒间隔。
        """
        interval = 30  # 默认 30 秒

        try:
            while self._ws:
                await asyncio.sleep(interval)
                if self._ws:
                    heartbeat = json.dumps({
                        "op": 1,      # 心跳操作码
                        "d": self._last_seq,  # 客户端目前收到的最新的消息序号
                    })
                    await self._ws.send(heartbeat)
                    logger.debug(f"[QQ] 心跳已发送 (seq={self._last_seq})")
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"[QQ] 心跳异常: {e}")

    # ─── 消息解析 ──────────────────────────────────────

    async def _handle_ws_message(self, raw_message: str | bytes) -> None:
        """处理 WebSocket 消息

        QQ Bot WebSocket 协议的操作码（op）：
        ┌─────┬────────────────────────────────┐
        │ op  │ 含义                           │
        ├─────┼────────────────────────────────┤
        │  0  │ Dispatch（服务端推送事件）      │
        │  1  │ Heartbeat（心跳）               │
        │  7  │ Reconnect（服务端要求重连）     │
        │  9  │ Invalid Session（会话失效）     │
        │ 10  │ Hello（连接建立，返回心跳间隔） │
        │ 11  │ Heartbeat ACK（心跳确认）       │
        └─────┴────────────────────────────────┘

        Args:
            raw_message: 原始 WebSocket 消息（字符串或字节）
        """
        try:
            # 处理可能的 bytes 类型
            if isinstance(raw_message, bytes):
                raw_message = raw_message.decode("utf-8")

            data = json.loads(raw_message)
            op = data.get("op", -1)
            seq = data.get("s")

            # 更新消息序号
            if seq is not None:
                self._last_seq = seq

            if op == 10:
                # Hello - 服务端确认连接，返回心跳间隔
                hello_data = data.get("d", {})
                self._session_id = hello_data.get("session_id", "")
                heartbeat_interval = hello_data.get("heartbeat_interval", 30000)
                logger.info(
                    f"[QQ] 服务端 Hello (session={self._session_id[:8]}..., "
                    f"heartbeat={heartbeat_interval}ms)"
                )

            elif op == 11:
                # Heartbeat ACK - 服务端确认收到心跳
                logger.debug("[QQ] 心跳 ACK")

            elif op == 7:
                # Reconnect - 服务端要求重连
                logger.warning("[QQ] 服务端要求重连 (op=7)")

            elif op == 9:
                # Invalid Session - 会话失效，需要重新鉴权
                logger.error("[QQ] 会话失效 (op=9)，尝试重新鉴权...")
                if await self._refresh_access_token():
                    logger.info("[QQ] 已重新获取 token，等待重连...")

            elif op == 0:
                # Dispatch - 事件分发
                event_type = data.get("t", "")
                event_data = data.get("d", {})

                if event_type in ("MESSAGE_CREATE", "AT_MESSAGE_CREATE"):
                    # 群聊消息 / @消息
                    await self._handle_message_event(event_data)

                elif event_type == "DIRECT_MESSAGE_CREATE":
                    # 私聊消息
                    await self._handle_message_event(event_data, is_private=True)

                elif event_type == "C2C_MESSAGE_CREATE":
                    # 单聊消息（v2 接口）
                    await self._handle_message_event(event_data, is_private=True)

                elif event_type == "GROUP_AT_MESSAGE_CREATE":
                    # 群聊 @ 消息（v2 接口）
                    await self._handle_message_event(event_data)

            else:
                logger.debug(f"[QQ] 未知 op={op}")

        except json.JSONDecodeError:
            logger.warning(f"[QQ] 无法解析 WebSocket 消息: {raw_message[:100]}")
        except Exception as e:
            logger.error(f"[QQ] 消息处理异常: {type(e).__name__}: {e}")

    async def _handle_message_event(
        self, event_data: dict, is_private: bool = False
    ) -> None:
        """处理消息事件，将 QQ 消息转为统一格式并分发

        QQ 消息的字段（以实际 API 文档为准）：
        - id: 消息 ID
        - author: { id: 用户ID, username: 用户名 }
        - content: 消息文本内容
        - channel_id / group_id: 群 ID
        - guild_id: 频道 ID（如果存在）
        - timestamp: 时间戳

        Args:
            event_data: QQ 推送的事件数据
            is_private: 是否为私聊消息
        """
        import re

        author = event_data.get("author", {})
        content = event_data.get("content", "")

        # 移除 @提及 标记（<@!userid> 或 <@userid> 格式）
        clean_content = re.sub(r"<@!?\w+>", "", content).strip()
        # 也移除常见的空白 @
        clean_content = re.sub(r"@\S+\s?", "", clean_content).strip()

        # 确定 chat_id
        if is_private:
            chat_id = author.get("id", "")
        else:
            # 群聊优先使用 group_id，其次 channel_id
            chat_id = (
                event_data.get("group_id")
                or event_data.get("channel_id")
                or ""
            )

        # 确定消息 ID
        message_id = event_data.get("id", "")

        msg = IMMessage(
            platform="qq",
            message_id=message_id,
            chat_id=chat_id,
            chat_type="private" if is_private else "group",
            sender_id=author.get("id", ""),
            sender_name=author.get("username", "未知用户"),
            content=clean_content,
            raw_content=event_data,
            reply_to=event_data.get("message_reference", {}).get("message_id"),
            timestamp=float(event_data.get("timestamp", time.time())),
        )

        logger.info(
            f"[QQ] 收到消息: sender={msg.sender_name}, "
            f"chat={chat_id[:12]}..., "
            f"content={clean_content[:50]}..."
        )

        # 分发到消息处理器
        await self._dispatch(msg)
