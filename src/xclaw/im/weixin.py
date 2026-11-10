"""Personal WeChat text messages over Tencent's iLink ClawBot protocol."""

from __future__ import annotations

import asyncio
import base64
import secrets
import time
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
import qrcode
from loguru import logger
from pydantic import BaseModel, Field

from xclaw.im.adapter import IMAdapter, IMMessage
from xclaw.persistence import atomic_json

_LOGIN_URL = "https://ilinkai.weixin.qq.com"
_VERSION = "2.4.10"
_BASE_INFO = {"channel_version": _VERSION, "bot_agent": "XClaw/0.1.0"}
_APP_HEADERS = {"iLink-App-Id": "bot", "iLink-App-ClientVersion": str(0x02040A)}


def _trusted_url(value: str) -> str:
    url = urlsplit(value)
    if (
        url.scheme != "https" or not (url.hostname or "").endswith(".weixin.qq.com")
        or url.username or url.password or url.port not in (None, 443)
        or url.path not in ("", "/") or url.query or url.fragment
    ):
        raise ValueError("iLink 返回了无效的微信服务地址")
    return value.rstrip("/")


def _post_headers(token: str = "") -> dict[str, str]:
    uin = base64.b64encode(str(secrets.randbits(32)).encode("ascii")).decode("ascii")
    headers = {**_APP_HEADERS, "AuthorizationType": "ilink_bot_token", "X-WECHAT-UIN": uin}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _response_data(response: httpx.Response) -> dict:
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict):
        raise ValueError("iLink 响应不是 JSON 对象")
    return data


class _Account(BaseModel):
    bot_token: str = Field(min_length=1, repr=False)
    account_id: str = Field(min_length=1)
    owner_id: str = ""
    base_url: str = _LOGIN_URL
    cursor: str = ""
    recent: list[str] = Field(default_factory=list)


async def login_weixin(path: Path) -> None:
    """Display a terminal QR code and save credentials after confirmation."""
    try:
        async with httpx.AsyncClient(timeout=45) as client:
            account = await _scan_qr(client)
    except (httpx.HTTPError, ValueError) as exc:
        # The QR status URL can include a login code in its query string.
        raise RuntimeError(f"iLink 登录请求失败（{type(exc).__name__}）") from None
    atomic_json(path.expanduser(), account.model_dump())
    print(f"微信已绑定。凭证保存到 {path}")


async def _scan_qr(client: httpx.AsyncClient) -> _Account:
    deadline = time.monotonic() + 480
    while time.monotonic() < deadline:
        qr = _response_data(await client.post(
            f"{_LOGIN_URL}/ilink/bot/get_bot_qrcode", params={"bot_type": "3"},
            headers=_post_headers(), json={"local_token_list": []},
        ))
        code, image = qr.get("qrcode"), qr.get("qrcode_img_content")
        if not isinstance(code, str) or not code or not isinstance(image, str) or not image:
            raise RuntimeError("iLink 未返回可用的二维码")
        print("用手机微信扫一扫，并在手机上确认绑定。")
        terminal_qr = qrcode.QRCode(border=2)
        terminal_qr.add_data(image)
        terminal_qr.print_ascii(invert=True)

        status_host = _LOGIN_URL
        params = {"qrcode": code}
        while time.monotonic() < deadline:
            try:
                status = _response_data(await client.get(
                    f"{status_host}/ilink/bot/get_qrcode_status",
                    headers=_APP_HEADERS, params=params,
                ))
            except httpx.ReadTimeout:
                continue
            params.pop("verify_code", None)
            match status.get("status"):
                case "confirmed":
                    return _Account(
                        bot_token=status.get("bot_token"), account_id=status.get("ilink_bot_id"),
                        owner_id=status.get("ilink_user_id") or "",
                        base_url=_trusted_url(status.get("baseurl") or status_host),
                    )
                case "expired":
                    print("二维码已过期，正在刷新。")
                    break
                case "scaned_but_redirect" | "binded_redirect" if status.get("redirect_host"):
                    status_host = _trusted_url(f"https://{status['redirect_host']}")
                case "need_verifycode":
                    code_input = (await asyncio.to_thread(input, "输入微信显示的验证码：")).strip()
                    if code_input:
                        params["verify_code"] = code_input
                case "verify_code_blocked":
                    raise RuntimeError("微信验证码尝试次数已用完，请重新运行登录命令")
            await asyncio.sleep(1)
    raise RuntimeError("扫码超时，请重新运行登录命令")


class WeixinAdapter(IMAdapter):
    def __init__(self, state_file: Path):
        super().__init__("weixin")
        self._state_file = state_file.expanduser()
        self._account: _Account | None = None
        self._client: httpx.AsyncClient | None = None
        self._poll_task: asyncio.Task | None = None
        self._reply_context: tuple[str, str, str] | None = None

    async def start(self) -> None:
        if self._poll_task is not None:
            return
        try:
            account = _Account.model_validate_json(self._state_file.read_bytes())
            account.base_url = _trusted_url(account.base_url)
        except (OSError, ValueError):
            raise RuntimeError("微信绑定状态不可用，请先运行 xclaw --weixin-login") from None
        self._account = account
        self._client = httpx.AsyncClient(timeout=15)
        self._poll_task = asyncio.create_task(self._poll(account))

    async def stop(self) -> None:
        if self._poll_task is not None:
            self._poll_task.cancel()
            await asyncio.gather(self._poll_task, return_exceptions=True)
            self._poll_task = None
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _request(self, method: str, data: dict, *, timeout: float = 15) -> dict:
        assert self._client is not None and self._account is not None
        response = await self._client.post(
            f"{self._account.base_url}/ilink/bot/{method}",
            headers=_post_headers(self._account.bot_token),
            json={**data, "base_info": _BASE_INFO}, timeout=timeout,
        )
        return _response_data(response)

    async def _poll(self, account: _Account) -> None:
        timeout, backoff = 45.0, 1
        dirty = False
        while True:
            try:
                if dirty:
                    atomic_json(self._state_file, account.model_dump())
                    dirty = False
                batch = await self._request(
                    "getupdates", {"get_updates_buf": account.cursor}, timeout=timeout,
                )
                if -14 in (batch.get("ret"), batch.get("errcode")):
                    logger.warning("[微信] iLink 会话过期，请重新扫码绑定后重启服务")
                    return
                if batch.get("ret", 0) != 0 or batch.get("errcode", 0) != 0:
                    raise RuntimeError("iLink 轮询返回错误")
                messages = batch.get("msgs") or []
                if not isinstance(messages, list):
                    raise ValueError("iLink 消息列表无效")
                for item in messages:
                    if isinstance(item, dict):
                        dirty = await self._handle_inbound(account, item) or dirty
                cursor = batch.get("get_updates_buf")
                if isinstance(cursor, str) and cursor and cursor != account.cursor:
                    account.cursor = cursor
                    dirty = True
                hint = batch.get("longpolling_timeout_ms")
                if isinstance(hint, (float, int)) and hint > 0:
                    timeout = min(max(hint / 1000 + 5, 10), 120)
                backoff = 1
            except httpx.ReadTimeout:
                continue
            except (httpx.HTTPError, OSError, ValueError, RuntimeError) as exc:
                logger.warning(f"[微信] 轮询或保存失败（{type(exc).__name__}），{backoff} 秒后重试")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)

    async def _handle_inbound(self, account: _Account, data: dict) -> bool:
        sender, context = data.get("from_user_id"), data.get("context_token")
        if (
            data.get("message_type", 1) != 1 or data.get("group_id")
            or not isinstance(sender, str) or not sender or sender == account.account_id
            or not isinstance(context, str) or not context
            or (account.owner_id and sender != account.owner_id)
        ):
            return False
        items = data.get("item_list")
        if not isinstance(items, list):
            return False
        parts = []
        for item in items:
            if isinstance(item, dict) and item.get("type") == 1:
                text_item = item.get("text_item")
                text = text_item.get("text") if isinstance(text_item, dict) else None
                if isinstance(text, str) and text.strip():
                    parts.append(text)
        if not parts:
            return False
        message_id = str(data.get("message_id") or data.get("seq") or data.get("client_id") or "")
        key = f"{sender}/{message_id}" if message_id else ""
        if key and key in account.recent:
            return False
        chat_id = f"{account.account_id}/{sender}"
        self._reply_context = (chat_id, sender, context)
        try:
            await self._dispatch(IMMessage(
                platform=self.name, message_id=message_id, chat_id=chat_id,
                chat_type="private", sender_id=sender, content="\n".join(parts),
            ))
        finally:
            self._reply_context = None
        if key:
            account.recent = [*account.recent[-255:], key]
        return bool(key)

    async def send_message(
        self, chat_id: str, content: str, *, reply_to: str | None = None,
        chat_type: str = "private",
    ) -> bool:
        if not self._reply_context or self._reply_context[0] != chat_id:
            logger.warning("[微信] 发送需要当前私聊消息的 context_token")
            return False
        _, sender, context = self._reply_context
        try:
            result = await self._request("sendmessage", {"msg": {
                "from_user_id": "", "to_user_id": sender, "client_id": f"xclaw-{uuid4().hex}",
                "message_type": 2, "message_state": 2, "context_token": context,
                "item_list": [{"type": 1, "text_item": {"text": self.truncate_message(content)}}],
            }})
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning(f"[微信] 回复请求失败（{type(exc).__name__}）")
            return False
        return result.get("ret", 0) == 0 and result.get("errcode", 0) == 0
