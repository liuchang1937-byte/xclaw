"""Relay OneBot 11 HTTP events to XClaw and send replies via the OneBot API."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route


def forward_payload(event: dict) -> dict | None:
    if event.get("post_type") != "message" or event.get("user_id") == event.get("self_id"):
        return None
    segments = event.get("message")
    if not isinstance(segments, list):
        return None
    group = event.get("message_type") == "group"
    if not event.get("user_id") or event.get("message_type") not in ("private", "group"):
        return None
    if group and (not event.get("group_id") or not any(
        segment.get("type") == "at" and str(segment.get("data", {}).get("qq"))
        == str(event.get("self_id")) for segment in segments
    )):
        return None
    content = "".join(
        segment.get("data", {}).get("text", "")
        for segment in segments if segment.get("type") == "text"
    ).strip()
    if not content:
        return None
    payload = {
        "user_id": str(event["user_id"]), "text": content,
        "message_id": str(event.get("message_id") or ""),
    }
    if group:
        payload["chat_id"] = str(event["group_id"])
    return payload


async def receive(request: Request) -> Response:
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > 128 * 1024:
            return Response(status_code=413)
        body.extend(chunk)
    secret = os.environ["ONEBOT_POST_SECRET"].encode("utf-8")
    digest = hmac.new(secret, body, hashlib.sha1).hexdigest()
    if not hmac.compare_digest(
        request.headers.get("x-signature", "").encode("utf-8"), f"sha1={digest}".encode("ascii")
    ):
        return Response(status_code=401)
    try:
        payload = forward_payload(json.loads(body))
    except (ValueError, AttributeError, TypeError):
        return Response(status_code=400)
    if payload is None:
        return Response(status_code=204)
    allowed = {value.strip() for value in os.environ["XCLAW_QQ_ALLOWED_USERS"].split(",")}
    if payload["user_id"] not in allowed:
        return Response(status_code=204)
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            response = await client.post(
                os.getenv("XCLAW_QQ_WEBHOOK", "http://127.0.0.1:8080/qq/events"),
                headers={"X-QQ-Webhook-Token": os.environ["XCLAW_QQ_BRIDGE_TOKEN"]}, json=payload,
            )
    except httpx.HTTPError:
        return Response(status_code=502)
    return Response(status_code=response.status_code)


def send() -> None:
    target = os.environ["MICROCLAW_QQ_TARGET"]
    group = os.environ["XCLAW_QQ_CHAT_TYPE"] == "group"
    action, id_key = ("send_group_msg", "group_id") if group else ("send_private_msg", "user_id")
    response = httpx.post(
        f"{os.environ['ONEBOT_API_URL'].rstrip('/')}/{action}",
        headers={"Authorization": f"Bearer {os.environ['ONEBOT_API_TOKEN']}"},
        json={id_key: int(target), "message": os.environ["MICROCLAW_QQ_TEXT"], "auto_escape": True},
        timeout=10,
    )
    response.raise_for_status()
    if response.json().get("retcode") != 0:
        raise SystemExit("OneBot 回复失败")


if __name__ == "__main__":
    if sys.argv[1:] == ["send"]:
        send()
    elif sys.argv[1:] == ["serve"]:
        for name in ("ONEBOT_POST_SECRET", "XCLAW_QQ_BRIDGE_TOKEN", "XCLAW_QQ_ALLOWED_USERS"):
            if not os.getenv(name, "").strip():
                raise SystemExit(f"请设置 {name}")
        app = Starlette(routes=[Route("/onebot/events", receive, methods=["POST"])])
        uvicorn.run(app, host="127.0.0.1", port=8765, access_log=False)
    else:
        raise SystemExit("用法: python examples/qq_onebot_bridge.py serve|send")
