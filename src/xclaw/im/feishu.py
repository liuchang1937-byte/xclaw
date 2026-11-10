"""
飞书适配器 - 接入飞书开放平台

支持三种接入方式：
1. Webhook 模式（最简单，5 分钟）：仅发送消息，不能接收
2. WebSocket 模式（推荐！不需要公网 IP）：通过飞书官方 SDK 长连接接收事件
3. HTTP 回调模式（需要公网）：通过 HTTP Server 接收事件回调

前置条件：
1. 在飞书开放平台 (https://open.feishu.cn) 创建一个企业自建应用
2. 获取 App ID 和 App Secret（在"凭证与基础信息"页面）
3. 开通消息相关权限并发布

使用示例：
    # WebSocket 模式（推荐，不需要公网）
    adapter = FeishuAdapter(
        app_id="cli_xxx",
        app_secret="xxx",
        mode="websocket",
    )

    # Webhook 模式（最简单，只能发不能收）
    adapter = FeishuAdapter(
        webhook_url="https://open.feishu.cn/open-apis/bot/v2/hook/xxx",
        mode="webhook",
    )

    adapter.on_message(handle_message)
    await adapter.start()
"""

from __future__ import annotations

import asyncio
import json
import mimetypes
import threading
import time
from contextvars import ContextVar
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
from loguru import logger

from xclaw.im.adapter import IMAdapter, IMMessage
from xclaw.im.delivery import validate_file


class FeishuAdapter(IMAdapter):
    """飞书适配器

    通过飞书开放平台 API 收发消息。

    三种模式：
    - webhook:   仅发送消息到群聊（最简单，5分钟配置）
    - websocket: WebSocket 长连接收发消息（推荐，使用官方 SDK，不需要公网 IP）
    - http:      HTTP 回调模式（需要公网 URL 和 HTTP Server）
    """

    BASE_URL = "https://open.feishu.cn/open-apis"
    supports_files = True

    def __init__(
        self,
        *,
        app_id: str = "",
        app_secret: str = "",
        verification_token: str = "",
        encrypt_key: str = "",
        webhook_url: str = "",
        mode: str = "websocket",
    ):
        """
        初始化飞书适配器

        Args:
            app_id: 飞书应用 ID（cli_xxxxxxxxxxxx）
            app_secret: 飞书应用密钥
            verification_token: 事件订阅验证 Token（仅 HTTP 回调模式需要）
            encrypt_key: 消息加密密钥（如果启用了加密）
            webhook_url: 机器人 Webhook 地址（仅 Webhook 模式需要）
            mode: 运行模式 - "webhook" | "websocket" | "http"
        """
        super().__init__(name="feishu")

        self.app_id = app_id
        self.app_secret = app_secret
        self.verification_token = verification_token
        self.encrypt_key = encrypt_key
        self.webhook_url = webhook_url
        self.mode = mode

        # Token 管理
        self._tenant_access_token: str = ""
        self._token_expires_at: float = 0

        # HTTP 客户端
        self._http_client: httpx.AsyncClient | None = None

        # WebSocket 模式（使用 lark_oapi 官方 SDK）
        self._ws_client: Any = None  # lark_oapi.ws.Client
        self._ws_thread: threading.Thread | None = None
        self._main_loop: asyncio.AbstractEventLoop | None = None
        self._file_failure: ContextVar[str] = ContextVar("feishu_file_failure", default="")

    # ─── 生命周期 ──────────────────────────────────────

    async def start(self) -> None:
        """启动飞书适配器"""
        # 捕获主应用程序的事件循环（用于 SDK 线程的消息桥接）
        self._main_loop = asyncio.get_running_loop()

        self._http_client = httpx.AsyncClient(timeout=30.0)

        # 获取 access_token
        if self.app_id and self.app_secret:
            if not await self._refresh_token():
                logger.error("[飞书] 无法获取 access_token")
                return
            logger.info(f"[飞书] Token 已获取 (App: {self.app_id[:8]}...)")

        if self.mode == "websocket":
            self._start_ws_sdk_mode()
        elif self.mode == "webhook" or self.webhook_url:
            logger.info("[飞书] Webhook 模式已启动")
        elif self.mode == "http":
            logger.info("[飞书] HTTP 回调模式已启动（需要配置公网 URL）")
        else:
            logger.warning("[飞书] 无有效凭据或模式配置")

    async def stop(self) -> None:
        """停止飞书适配器"""
        # 停止 WebSocket
        if self._ws_thread and self._ws_thread.is_alive():
            logger.info("[飞书] 等待 WebSocket 线程退出...")
            self._ws_thread.join(timeout=5)

        # 关闭 HTTP 客户端
        if self._http_client:
            await self._http_client.aclose()
            self._http_client = None

        logger.info("[飞书] 适配器已停止")

    # ─── WebSocket 模式（使用 lark_oapi 官方 SDK）───────

    def _start_ws_sdk_mode(self) -> None:
        """使用飞书官方 SDK 启动 WebSocket 长连接

        lark_oapi 的 WS Client 内部使用自己的 asyncio 事件循环（阻塞式）。
        由于主线程已经有 asyncio 事件循环在运行，我们必须：
        1. 在子线程中导入 lark_oapi（它在模块加载时获取事件循环）
        2. 在子线程中创建 Client 并调用 start()
        3. 通过线程安全的回调将消息桥接回主线程的 asyncio 世界
        """
        # 在子线程中完成所有 lark_oapi 的操作
        # （因为 lark_oapi 的 ws 模块在 import 时会 get_event_loop()，
        #   必须在一个没有运行中的事件循环的线程里导入）
        self._ws_thread = threading.Thread(
            target=self._run_ws_sdk,
            name="feishu-ws",
            daemon=True,
        )
        self._ws_thread.start()

        logger.info("[飞书] WebSocket 模式已启动（SDK 长连接，不需要公网 URL）")

    def _run_ws_sdk(self) -> None:
        """在独立线程中完成 lark_oapi 导入、Client 创建和启动

        关键：所有 lark_oapi 相关的导入和调用都在子线程中进行，
        确保 SDK 在自己的事件循环中运行，不与主线程冲突。
        """
        try:
            # 导入放在子线程中 —— lark_oapi 的 ws 模块在 import 时
            # 会调用 asyncio.get_event_loop()，此时子线程没有运行中的循环，
            # 触发 RuntimeError 后 SDK 会自己 new_event_loop()。
            from lark_oapi.event.dispatcher_handler import EventDispatcherHandler
            from lark_oapi.ws import Client

            # 创建事件处理器 —— 同时注册 p1 和 p2 协议版本
            handler = (
                EventDispatcherHandler.builder(
                    encrypt_key=self.encrypt_key or "",
                    verification_token=self.verification_token or "",
                )
                .register_p1_customized_event(
                    "im.message.receive_v1",
                    self._on_fs_message_event,
                )
                .register_p2_customized_event(
                    "im.message.receive_v1",
                    self._on_fs_message_event,
                )
                .build()
            )

            # 创建 WS Client
            self._ws_client = Client(
                app_id=self.app_id,
                app_secret=self.app_secret,
                event_handler=handler,
                auto_reconnect=True,
            )
            self._ws_client.on_reconnecting = lambda: logger.info(
                "[飞书] WebSocket 正在重连..."
            )
            self._ws_client.on_reconnected = lambda: logger.info(
                "[飞书] WebSocket 已重连"
            )

            # 启动（阻塞，直到连接断开）
            logger.info("[飞书] 正在建立 WebSocket 长连接（SDK）...")
            self._ws_client.start()

        except ImportError:
            logger.error(
                "[飞书] 缺少 lark-oapi 依赖！\n"
                "   请运行: uv pip install lark-oapi"
            )
        except Exception as e:
            logger.error(
                f"[飞书] SDK WebSocket 致命错误: {type(e).__name__}: {e}"
            )

    def _on_fs_message_event(self, event: Any) -> None:
        """处理飞书 SDK 推送的 im.message.receive_v1 事件

        Args:
            event: lark_oapi 的 CustomizedEvent 对象
        """
        try:
            # 提取原始事件数据
            raw_event = getattr(event, "event", {}) or {}

            # 飞书 SDK 长连接推送的 p2 事件格式：
            #   {message: {message_type, content, chat_id, ...}, sender: {...}}
            # p1 事件则多一层嵌套：
            #   {type: "...", event: {message: {...}, sender: {...}}}
            inner = raw_event.get("event", {}) if isinstance(raw_event, dict) else {}
            if not inner:
                inner = raw_event

            message_data = inner.get("message", {})
            sender = inner.get("sender", {})

            # 提取发送者 ID
            sender_id = ""
            if isinstance(sender, dict):
                sender_info = sender.get("sender_id", {})
                if isinstance(sender_info, dict):
                    sender_id = sender_info.get("open_id", "")
                elif isinstance(sender_info, str):
                    sender_id = sender_info
                elif isinstance(sender, str):
                    sender_id = sender

            # 提取消息 ID
            message_id = ""
            if isinstance(message_data, dict):
                message_id = message_data.get("message_id", "")

            # 提取会话 ID
            chat_id = ""
            if isinstance(message_data, dict):
                chat_id = message_data.get("chat_id", "")

            # 提取文本内容
            content = self._extract_text(message_data if isinstance(message_data, dict) else {})

            # 构建统一消息
            msg = IMMessage(
                platform="feishu",
                message_id=message_id,
                chat_id=chat_id,
                chat_type=(
                    message_data.get("chat_type", "group")
                    if isinstance(message_data, dict) else "group"
                ),
                sender_id=sender_id,
                sender_name="",
                content=content,
                attachments=self._extract_attachments(message_data),
                raw_content=message_data if isinstance(message_data, dict) else {},
                reply_to=message_data.get("root_id") if isinstance(message_data, dict) else None,
                timestamp=time.time(),
            )

            logger.info(
                f"[飞书] 收到消息: sender={sender_id[:8]}..., "
                f"content={content[:50]}..."
            )

            # 安全地桥接到 asyncio 事件循环
            self._dispatch_safe(msg)

        except Exception as e:
            logger.error(f"[飞书] SDK 事件处理异常: {type(e).__name__}: {e}")

    def _dispatch_safe(self, msg: IMMessage) -> None:
        """将 SDK 线程中的消息安全桥接到主应用的事件循环

        SDK 的回调在自己的线程 + 自己的事件循环中执行，
        而 Agent 和 HTTP 客户端都在主线程的 asyncio 循环中运行。
        因此必须用 run_coroutine_threadsafe 把消息分发到主循环。

        Args:
            msg: 要分发的消息
        """
        if self._main_loop is None:
            logger.error("[飞书] 主事件循环未设置，消息无法分发")
            return

        if not self._main_loop.is_running():
            logger.warning("[飞书] 主事件循环未运行，消息可能丢失")
            return

        try:
            logger.info(
                f"[飞书] 桥接消息到主循环: chat_id={msg.chat_id[:20]}..., "
                f"content={msg.content[:50]}..."
            )
            asyncio.run_coroutine_threadsafe(
                self._dispatch(msg), self._main_loop
            )
        except Exception as e:
            logger.error(f"[飞书] 线程桥接失败: {type(e).__name__}: {e}")

    # ─── 消息发送 ──────────────────────────────────────

    async def send_message(
        self,
        chat_id: str,
        content: str,
        *,
        reply_to: str | None = None,
        chat_type: str = "group",
    ) -> bool:
        """发送消息到飞书"""
        if self.webhook_url:
            return await self._send_via_webhook(content)
        elif self._tenant_access_token:
            return await self._send_via_api(chat_id, content, reply_to, chat_type)
        else:
            logger.error("[飞书] 无法发送消息：缺少 webhook_url 或 access_token")
            return False

    async def _send_via_webhook(self, content: str) -> bool:
        """通过 Webhook 发送消息"""
        if not self._http_client:
            return False
        try:
            payload = {
                "msg_type": "interactive",
                "card": {
                    "header": {
                        "title": {"tag": "plain_text", "content": "XClaw Agent"},
                        "template": "blue",
                    },
                    "elements": [
                        {
                            "tag": "markdown",
                            "content": self.truncate_message(content, 3000),
                        }
                    ],
                },
            }
            response = await self._http_client.post(self.webhook_url, json=payload)
            if response.status_code == 200:
                result = response.json()
                if result.get("code") == 0:
                    logger.debug("[飞书] Webhook 消息已发送")
                    return True
                else:
                    logger.error(f"[飞书] Webhook 发送失败: {result}")
            return False
        except Exception as e:
            logger.error(f"[飞书] Webhook 发送异常: {e}")
            return False

    async def send_file(
        self, chat_id: str, path: Path, *, reply_to: str | None = None,
        chat_type: str = "group",
    ) -> bool:
        """上传真实文件，再用 file_key 发送文件消息；不会把本地路径发给用户。"""
        self._file_failure.set("")
        stage = "文件上传"
        if self.mode == "webhook" or self._http_client is None:
            self._file_failure.set("需要应用机器人 API，Webhook 模式不支持文件")
            logger.error("[飞书] 文件发送需要应用机器人 API 与已启动的 HTTP 客户端")
            return False
        try:
            validate_file(path)
            if time.time() > self._token_expires_at and not await self._refresh_token():
                self._file_failure.set("飞书访问令牌获取失败")
                return False
            if not self._tenant_access_token:
                self._file_failure.set("飞书访问令牌不存在")
                return False
            headers = {"Authorization": f"Bearer {self._tenant_access_token}"}
            kind = {
                ".xlsx": "xls", ".xls": "xls", ".docx": "doc", ".doc": "doc",
                ".pdf": "pdf", ".pptx": "ppt", ".ppt": "ppt",
            }.get(path.suffix.lower(), "stream")
            mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            with path.open("rb") as file:
                response = await self._http_client.post(
                    f"{self.BASE_URL}/im/v1/files",
                    headers=headers,
                    data={"file_type": kind, "file_name": path.name},
                    files={"file": (path.name, file, mime)},
                )
            if self._file_api_failed(response, stage):
                return False
            result = response.json()
            file_key = result.get("data", {}).get("file_key")
            if result.get("code") != 0 or not file_key:
                self._file_failure.set("文件上传接口未返回 file_key")
                logger.error(f"[飞书] 文件上传失败，code={result.get('code')}")
                return False
            payload = {"msg_type": "file", "content": json.dumps({"file_key": file_key})}
            stage = "文件消息发送"
            if reply_to:
                url = f"{self.BASE_URL}/im/v1/messages/{reply_to}/reply"
                params = None
            else:
                url = f"{self.BASE_URL}/im/v1/messages"
                params = {"receive_id_type": "chat_id"}
                payload["receive_id"] = chat_id
            response = await self._http_client.post(
                url, params=params, headers=headers, json=payload,
            )
            if self._file_api_failed(response, stage):
                return False
            result = response.json()
            if result.get("code") != 0:
                logger.error(f"[飞书] 文件消息发送失败，code={result.get('code')}")
                return False
            logger.info(f"[飞书] 最终文件已发送: {path.name}")
            return True
        except Exception as exc:
            self._file_failure.set(f"{stage}失败：{type(exc).__name__}")
            logger.error(f"[飞书] {stage}失败: {type(exc).__name__}")
            return False

    def file_failure_reason(self) -> str:
        return self._file_failure.get()

    def _file_api_failed(self, response: httpx.Response, stage: str) -> bool:
        """先读取业务错误再处理 HTTP 状态，保留 400/403 中的权限错误码。"""
        try:
            result = response.json()
            if not isinstance(result, dict):
                result = {}
        except (ValueError, UnicodeError):
            result = {}
        code = result.get("code")
        if response.is_success and code == 0:
            return False
        raw_msg = str(result.get("msg", ""))
        for secret in (self.app_secret, self._tenant_access_token):
            if secret:
                raw_msg = raw_msg.replace(secret, "[redacted]")
        error = result.get("error") or {}
        log_id = error.get("log_id", "") if isinstance(error, dict) else ""
        logger.error(
            f"[飞书] {stage}失败: HTTP={response.status_code}, code={code}, "
            f"msg={raw_msg[:1000]}, log_id={log_id}"
        )
        if code == 99991672:
            scope = "im:message:send_as_bot" if stage == "文件消息发送" else "im:resource"
            if "im:resource" in raw_msg:
                scope = "im:resource"
            detail = f"缺少 {scope} 权限，请在飞书权限管理开通并发布新版本（99991672）"
        else:
            detail = f"{stage}失败，HTTP {response.status_code}，错误码 {code or '未知'}"
        self._file_failure.set(detail)
        return True

    async def _send_via_api(
        self,
        chat_id: str,
        content: str,
        reply_to: str | None = None,
        chat_type: str = "group",
    ) -> bool:
        """通过飞书 API 发送消息"""
        if not self._http_client:
            logger.error("[飞书] HTTP 客户端未初始化")
            return False
        if time.time() > self._token_expires_at:
            if not await self._refresh_token():
                return False
        if not self._tenant_access_token:
            logger.error("[飞书] 无 access_token")
            return False

        try:
            msg_content = json.dumps({"text": self.truncate_message(content)})
            # 注意：飞书私聊的 chat_type 是 "p2p"，仍应该用 chat_id
            receive_id_type = "chat_id" if chat_type in ("group", "p2p", "private") else "open_id"
            payload = {
                "receive_id": chat_id,
                "msg_type": "text",
                "content": msg_content,
            }
            if reply_to:
                payload["root_id"] = reply_to

            logger.info(
                f"[飞书] 发送消息: chat_id={chat_id}, "
                f"receive_id_type={receive_id_type}, "
                f"content_len={len(content)}"
            )

            response = await self._http_client.post(
                f"{self.BASE_URL}/im/v1/messages",
                params={"receive_id_type": receive_id_type},
                headers={
                    "Authorization": f"Bearer {self._tenant_access_token}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )

            logger.info(
                f"[飞书] API 响应: status={response.status_code}, "
                f"body={response.text[:200]}"
            )

            if response.status_code == 200:
                result = response.json()
                if result.get("code") == 0:
                    logger.info("[飞书] API 消息发送成功！")
                    return True
                else:
                    logger.error(
                        f"[飞书] API 返回错误: code={result.get('code')}, "
                        f"msg={result.get('msg', '')}"
                    )
                    return False
            else:
                logger.error(f"[飞书] API HTTP 错误: {response.status_code}")
                return False
        except Exception as e:
            logger.error(f"[飞书] API 发送异常: {type(e).__name__}: {e}")
            return False

    # ─── Token 管理 ────────────────────────────────────

    async def _refresh_token(self) -> bool:
        """刷新 tenant_access_token"""
        if not self.app_id or not self.app_secret:
            logger.error("[飞书] App ID 或 App Secret 未配置")
            return False

        client = self._http_client or httpx.AsyncClient(timeout=30.0)
        should_close = self._http_client is None

        try:
            response = await client.post(
                f"{self.BASE_URL}/auth/v3/tenant_access_token/internal",
                json={"app_id": self.app_id, "app_secret": self.app_secret},
            )
            if response.status_code == 200:
                data = response.json()
                if data.get("code") == 0:
                    self._tenant_access_token = data["tenant_access_token"]
                    expire = int(data.get("expire", 7200))
                    self._token_expires_at = time.time() + expire - 300
                    logger.info(f"[飞书] Token 已刷新 (有效期 {expire}s)")
                    return True
                else:
                    logger.error(f"[飞书] Token 刷新失败: {data}")
            return False
        except Exception as e:
            logger.error(f"[飞书] Token 刷新异常: {e}")
            return False
        finally:
            if should_close:
                await client.aclose()

    # ─── HTTP 回调处理 ─────────────────────────────────

    def handle_event_callback(self, body: dict) -> dict | None:
        """处理飞书 HTTP 事件回调（mode="http" 时使用）"""
        if body.get("type") == "url_verification":
            token = body.get("token", "")
            challenge = body.get("challenge", "")
            if self.verification_token and token != self.verification_token:
                logger.warning("[飞书] URL 验证 token 不匹配")
                return None
            logger.info("[飞书] URL 验证成功")
            return {"challenge": challenge}

        header = body.get("header", {})
        event = body.get("event", {})
        event_type = header.get("event_type", "")

        if event_type == "im.message.receive_v1":
            message_data = event.get("message", {})
            sender = event.get("sender", {})
            msg = IMMessage(
                platform="feishu",
                message_id=message_data.get("message_id", ""),
                chat_id=message_data.get("chat_id", ""),
                chat_type=message_data.get("chat_type", "group"),
                sender_id=sender.get("sender_id", {}).get("open_id", ""),
                sender_name="",
                content=self._extract_text(message_data),
                attachments=self._extract_attachments(message_data),
                raw_content=message_data,
                reply_to=message_data.get("root_id"),
                timestamp=float(header.get("create_time", time.time())),
            )
            import asyncio
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(self._dispatch(msg))
            except RuntimeError:
                pass
            return {}
        return {}

    # ─── 消息解析 ──────────────────────────────────────

    @staticmethod
    def _extract_attachments(message: dict) -> list[dict]:
        if not isinstance(message, dict):
            return []
        if (message.get("msg_type") or message.get("message_type")) != "file":
            return []
        try:
            content = message.get("content", {})
            if isinstance(content, str):
                content = json.loads(content)
            key = content.get("file_key")
            if not isinstance(key, str) or not key:
                return []
            return [{"type": "file", "file_key": key,
                     "name": str(content.get("file_name") or "附件.bin")}]
        except (ValueError, AttributeError):
            return []

    async def download_file(self, message_id: str, file_key: str, target: Path) -> bool:
        """通过原消息资源接口下载附件，不使用只能下载机器人自有文件的接口。"""
        from tempfile import NamedTemporaryFile

        from xclaw.im.delivery import MAX_FILE_BYTES

        self._file_failure.set("")
        temporary = None
        try:
            if self._http_client is None:
                raise ValueError("飞书 HTTP 客户端未启动")
            if time.time() > self._token_expires_at and not await self._refresh_token():
                self._file_failure.set("飞书访问令牌获取失败")
                return False
            if not self._tenant_access_token:
                self._file_failure.set("飞书访问令牌不存在")
                return False
            url = (
                f"{self.BASE_URL}/im/v1/messages/{quote(message_id, safe='')}/resources/"
                f"{quote(file_key, safe='')}"
            )
            async with self._http_client.stream(
                "GET", url, params={"type": "file"},
                headers={"Authorization": f"Bearer {self._tenant_access_token}"},
            ) as response:
                if not response.is_success:
                    await response.aread()
                    self._file_api_failed(response, "文件下载")
                    return False
                if "application/json" in response.headers.get("Content-Type", ""):
                    await response.aread()
                    result = response.json()
                    if isinstance(result, dict) and result.get("code") not in (None, 0):
                        self._file_api_failed(response, "文件下载")
                        return False
                if int(response.headers.get("Content-Length", 0)) > MAX_FILE_BYTES:
                    raise ValueError("附件超过 30 MB")
                target.parent.mkdir(parents=True, exist_ok=True)
                with NamedTemporaryFile(
                    dir=target.parent, suffix=target.suffix or ".bin", delete=False,
                ) as file:
                    temporary = Path(file.name)
                    size = 0
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > MAX_FILE_BYTES:
                            raise ValueError("附件超过 30 MB")
                        file.write(chunk)
                validate_file(temporary)
                temporary.replace(target)
            return True
        except Exception as exc:
            self._file_failure.set(f"文件下载失败：{type(exc).__name__}")
            logger.error(f"[飞书] 文件下载失败: {type(exc).__name__}")
            return False
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def _extract_text(self, message: dict) -> str:
        """从飞书消息中提取纯文本"""
        if not isinstance(message, dict):
            return str(message)

        # SDK 长连接用 message_type，HTTP 回调用 msg_type
        msg_type = message.get("msg_type") or message.get("message_type", "")
        if msg_type == "file":
            attachments = self._extract_attachments(message)
            if attachments:
                return f"[文件附件：{attachments[0]['name']}]"
            return "[文件消息：元数据无效]"
        if msg_type == "text":
            content = message.get("content", "{}")
            try:
                return json.loads(content).get("text", "")
            except (json.JSONDecodeError, AttributeError):
                return str(content)

        type_descriptions = {
            "image": "[图片消息]",
            "file": "[文件消息]",
            "audio": "[语音消息]",
            "media": "[媒体消息]",
            "sticker": "[表情包]",
            "post": "[富文本消息]",
            "interactive": "[交互卡片]",
        }
        return type_descriptions.get(msg_type, f"[{msg_type} 消息]")
