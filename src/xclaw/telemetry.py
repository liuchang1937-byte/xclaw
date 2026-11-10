"""Task-scoped JSONL telemetry. Content capture is opt-in for evaluation fixtures."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import secrets
import threading
import time
import uuid
from contextlib import aclosing, contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path

from loguru import logger

_current: ContextVar[RunTrace | None] = ContextVar("xclaw_trace", default=None)
_purpose: ContextVar[str] = ContextVar("xclaw_model_purpose", default="main")


class RunTrace:
    def __init__(
        self, directory: str | Path | None = None, *, capture_content=False, content_limit=256_000
    ):
        self.run_id = uuid.uuid4().hex
        self.path = Path(directory) / f"{self.run_id}.jsonl" if directory else None
        self.events: list[dict] = []
        self.started = time.perf_counter()
        self._lock = threading.Lock()
        self.write_failed = False
        self._fingerprint_key = secrets.token_bytes(32)
        self.capture_content = capture_content
        self.content_limit = content_limit

    def capture(self, event: str, **data) -> None:
        if not self.capture_content:
            return
        encoded = json.dumps(
            data,
            ensure_ascii=False,
            default=lambda obj: obj.model_dump(mode="json"),
        )
        truncated = len(encoded) > self.content_limit
        self.emit(event, payload=None if truncated else json.loads(encoded), truncated=truncated)

    def argument_fingerprint(self, tool: str, arguments: str) -> str:
        try:
            canonical = json.dumps(
                json.loads(arguments),
                sort_keys=True,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            )
        except (ValueError, TypeError):
            canonical = arguments
        return hmac.new(
            self._fingerprint_key, (tool + "\0" + canonical).encode(), hashlib.sha256
        ).hexdigest()

    def emit(self, event: str, **data) -> None:
        row = {
            "run_id": self.run_id,
            "event": event,
            "time": datetime.now(timezone.utc).isoformat(),
            "elapsed_seconds": time.perf_counter() - self.started,
            **data,
        }
        with self._lock:
            self.events.append(row)
            if self.path and not self.write_failed:
                try:
                    self.path.parent.mkdir(parents=True, exist_ok=True)
                    with self.path.open("a", encoding="utf-8") as stream:
                        stream.write(json.dumps(row, ensure_ascii=False) + "\n")
                except OSError as exc:
                    self.write_failed = True
                    logger.warning("执行记录落盘失败: {}", type(exc).__name__)

    def metrics(self) -> dict:
        calls = [e for e in self.events if e["event"] == "model.finished"]
        known = [e["usage"] for e in calls if e.get("usage") is not None]
        tools = [e for e in self.events if e["event"] == "tool.finished"]
        return {
            "run_id": self.run_id,
            "trace_write_failed": self.write_failed,
            "model_calls": len(calls),
            "model_errors": sum(e["status"] != "ok" for e in calls),
            "usage_complete": len(known) == len(calls),
            "reported_tokens": sum(u["total_tokens"] for u in known),
            "prompt_tokens": sum(u["prompt_tokens"] for u in known),
            "completion_tokens": sum(u["completion_tokens"] for u in known),
            "calls_by_purpose": {
                purpose: sum(e["purpose"] == purpose for e in calls)
                for purpose in sorted({e["purpose"] for e in calls})
            },
            "tool_calls": len(tools),
            "tool_errors": sum(e["status"] != "ok" for e in tools),
            "tool_skipped": sum(e["event"] == "tool.skipped" for e in self.events),
        }


@contextmanager
def activate(trace: RunTrace):
    token = _current.set(trace)
    try:
        yield trace
    finally:
        _current.reset(token)


@contextmanager
def model_purpose(purpose: str):
    token = _purpose.set(purpose)
    try:
        yield
    finally:
        _purpose.reset(token)


def emit(event: str, **data) -> None:
    trace = _current.get()
    if trace:
        trace.emit(event, **data)


def capture(event: str, **data) -> None:
    trace = _current.get()
    if trace:
        trace.capture(event, **data)


def current_model_purpose() -> str:
    return _purpose.get()


def argument_fingerprint(tool: str, arguments: str) -> str | None:
    trace = _current.get()
    return trace.argument_fingerprint(tool, arguments) if trace else None


async def observed_chat(provider, messages, tools=None, *, purpose: str | None = None, **kwargs):
    call_id = uuid.uuid4().hex
    started = time.perf_counter()
    category = purpose or _purpose.get()
    emit("model.started", call_id=call_id, purpose=category, model=kwargs.get("model"))
    capture(
        "model.request",
        call_id=call_id,
        purpose=category,
        messages=messages,
        tools=tools,
        settings=kwargs,
    )
    try:
        with model_purpose(category):
            response = await provider.chat(messages, tools, **kwargs)
    except BaseException as exc:
        emit(
            "model.finished",
            call_id=call_id,
            purpose=category,
            status="cancelled" if isinstance(exc, asyncio.CancelledError) else "error",
            error_type=type(exc).__name__,
            http_status=getattr(exc, "status_code", None),
            duration_seconds=time.perf_counter() - started,
            usage=None,
        )
        raise
    capture("model.response", call_id=call_id, purpose=category, response=response)
    emit(
        "model.finished",
        call_id=call_id,
        purpose=category,
        status="ok",
        duration_seconds=time.perf_counter() - started,
        usage=response.usage.model_dump() if response.usage else None,
        finish_reason=response.finish_reason,
    )
    return response


async def observed_stream(provider, messages, tools=None, **kwargs):
    call_id = uuid.uuid4().hex
    started = time.perf_counter()
    category = _purpose.get()
    usage = None
    status = "cancelled"
    emit("model.started", call_id=call_id, purpose=category, model=kwargs.get("model"))
    try:
        async with aclosing(provider.chat_stream(messages, tools, **kwargs)) as stream:
            async for chunk in stream:
                if chunk.usage is not None:
                    usage = chunk.usage.model_dump()
                yield chunk
        status = "ok"
    except BaseException as exc:
        status = (
            "cancelled" if isinstance(exc, (asyncio.CancelledError, GeneratorExit)) else "error"
        )
        raise
    finally:
        emit(
            "model.finished",
            call_id=call_id,
            purpose=category,
            status=status,
            duration_seconds=time.perf_counter() - started,
            usage=usage,
        )
