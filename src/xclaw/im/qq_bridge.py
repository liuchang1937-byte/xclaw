"""QQ bridge with the MicroClaw webhook and send-command conventions."""

from __future__ import annotations

import asyncio
import hmac
import os
import socket
from contextlib import contextmanager

import uvicorn
from loguru import logger
from pydantic import BaseModel, Field, ValidationError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from xclaw.im.adapter import IMAdapter, IMMessage


class _IncomingMessage(BaseModel):
    user_id: str = Field(min_length=1, max_length=256)
    text: str = Field(min_length=1, max_length=8000)
    message_id: str = Field(default="", max_length=256)
    chat_id: str = Field(default="", max_length=256)


class _BridgeServer(uvicorn.Server):
    @contextmanager
    def capture_signals(self):
        # XClaw owns SIGINT/SIGTERM handling.
        yield


class QQBridgeAdapter(IMAdapter):
    def __init__(self, *, host: str, port: int, token: str, send_command: list[str]):
        super().__init__("qq_bridge")
        if not token.strip() or not send_command or not send_command[0].strip():
            raise ValueError("QQ 桥接需要配置 qq_bridge_token 和 qq_bridge_send_command")
        self.host = host
        self.port = port
        self._token = token.encode("utf-8")
        self._send_command = send_command
        self._queue: asyncio.Queue[IMMessage] = asyncio.Queue(maxsize=32)
        self._recent: dict[tuple[str, str, str], None] = {}
        self.app = Starlette(routes=[Route("/qq/events", self._receive, methods=["POST"])])
        self._server = _BridgeServer(uvicorn.Config(
            self.app, host=host, port=port, log_config=None, access_log=False,
            ws="none", lifespan="off", timeout_graceful_shutdown=5,
        ))
        self._serve_task: asyncio.Task | None = None
        self._worker: asyncio.Task | None = None

    async def start(self) -> None:
        if self._serve_task is not None:
            return
        family = socket.AF_INET6 if ":" in self.host else socket.AF_INET
        listener = socket.create_server((self.host, self.port), family=family)
        try:
            self._server.should_exit = False
            self._server.started = False
            self._serve_task = asyncio.create_task(self._server.serve(sockets=[listener]))
            while not self._server.started and not self._serve_task.done():
                await asyncio.sleep(0.01)
            if not self._server.started:
                await self._serve_task
                raise RuntimeError("QQ 桥接 HTTP 服务启动失败")
            self._worker = asyncio.create_task(self._consume())
        except BaseException:
            await self.stop()
            listener.close()
            raise

    async def stop(self) -> None:
        self._server.should_exit = True
        try:
            if self._serve_task is not None:
                await asyncio.gather(self._serve_task, return_exceptions=True)
                self._serve_task = None
        finally:
            if self._worker is not None:
                self._worker.cancel()
                await asyncio.gather(self._worker, return_exceptions=True)
                self._worker = None

    async def _receive(self, request: Request) -> JSONResponse:
        supplied = request.headers.get("x-qq-webhook-token", "").encode("utf-8")
        if not hmac.compare_digest(supplied, self._token):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > 32 * 1024:
                return JSONResponse({"error": "payload too large"}, status_code=413)
            body.extend(chunk)
        try:
            payload = _IncomingMessage.model_validate_json(body)
        except ValidationError:
            return JSONResponse({"error": "invalid message"}, status_code=400)
        if not payload.text.strip() or not payload.user_id.strip():
            return JSONResponse({"error": "empty user_id or text"}, status_code=400)
        chat_id = payload.chat_id.strip() or payload.user_id.strip()
        chat_type = "group" if payload.chat_id.strip() else "private"
        key = (chat_type, chat_id, payload.message_id)
        if payload.message_id and key in self._recent:
            return JSONResponse({"status": "duplicate"}, status_code=202)
        message = IMMessage(
            platform=self.name, message_id=payload.message_id, chat_id=chat_id,
            chat_type=chat_type, sender_id=payload.user_id.strip(), content=payload.text,
        )
        try:
            self._queue.put_nowait(message)
        except asyncio.QueueFull:
            return JSONResponse({"error": "queue full"}, status_code=503)
        if payload.message_id:
            self._recent[key] = None
            if len(self._recent) > 1024:
                self._recent.pop(next(iter(self._recent)))
        return JSONResponse({"status": "accepted"}, status_code=202)

    async def _consume(self) -> None:
        while True:
            await self._dispatch(await self._queue.get())

    async def send_message(
        self, chat_id: str, content: str, *, reply_to: str | None = None,
        chat_type: str = "group",
    ) -> bool:
        if "\x00" in content or "\x00" in chat_id or "\x00" in (reply_to or ""):
            return False
        env = dict(os.environ)
        env.update(
            MICROCLAW_QQ_TARGET=chat_id,
            MICROCLAW_QQ_TEXT=self.truncate_message(content),
            XCLAW_QQ_CHAT_TYPE=chat_type,
            XCLAW_QQ_REPLY_TO=reply_to or "",
        )
        try:
            process = await asyncio.create_subprocess_exec(
                *self._send_command, env=env, stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
        except OSError as exc:
            logger.error(f"[QQ 桥接] 无法启动回复命令：{exc}")
            return False
        try:
            code = await asyncio.wait_for(process.wait(), timeout=20)
        except (asyncio.TimeoutError, asyncio.CancelledError) as exc:
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                await process.wait()
            if isinstance(exc, asyncio.CancelledError):
                raise
            logger.error("[QQ 桥接] 回复命令超时")
            return False
        if code != 0:
            logger.error(f"[QQ 桥接] 回复命令退出码：{code}")
        return code == 0
