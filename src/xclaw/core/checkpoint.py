"""Durable run state and per-tool journal; never silently replay uncertain writes."""

from __future__ import annotations

import json
import os
import re
from contextlib import contextmanager
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from xclaw.ai.types import ChatMessage, ToolCall, ToolResult
from xclaw.core.scheduling import BatchPlan, tool_failed
from xclaw.persistence import atomic_json


class UncertainToolExecutionError(RuntimeError):
    """An interrupted side effect requires inspection and an explicit result."""


class RunSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    run_id: str
    workspace: str
    fingerprint: str
    user_input: str
    metadata: dict[str, str] = Field(default_factory=dict)
    tools: list[str] | None = None
    completion_required: bool = False
    iteration: int = Field(default=0, ge=0)
    phase: Literal["ready", "tools", "finished"] = "ready"
    status: str = "running"
    last_error: str = ""
    messages: list[ChatMessage] = Field(default_factory=list)
    history: list[ChatMessage] = Field(default_factory=list)
    memory: list[dict] = Field(default_factory=list)
    memory_scope: str = ""
    summary: str = ""
    total_tokens: int = 0
    total_calls: int = 0
    latest_output: str = ""
    deliveries: list[str] = Field(default_factory=list)
    plan: BatchPlan | None = None
    started: list[str] = Field(default_factory=list)
    results: dict[str, ToolResult] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_pending_tools(self):
        if self.phase == "tools":
            if not self.messages or self.messages[-1].role != "assistant":
                raise ValueError("工具阶段缺少助手调用消息")
            calls = self.messages[-1].tool_calls or []
            ids = {call.id for call in calls}
            if not calls or len(ids) != len(calls):
                raise ValueError("工具阶段调用 ID 为空或重复")
            if not set(self.started) <= ids or not self.results.keys() <= ids:
                raise ValueError("工具执行记录包含未知调用")
            for key, result in self.results.items():
                if key != result.tool_call_id:
                    raise ValueError("工具结果 ID 不匹配")
            if self.plan:
                self.plan.validate_calls(calls)
        return self


class CheckpointStore:
    def __init__(self, directory: str | Path):
        self.directory = Path(directory).resolve()

    def path(self, run_id: str) -> Path:
        if not re.fullmatch(r"[a-f0-9]{32}", run_id):
            raise ValueError("无效 checkpoint ID")
        return self.directory / f"{run_id}.json"

    def load(self, run_id: str) -> RunSnapshot:
        state = RunSnapshot.model_validate_json(self.path(run_id).read_text(encoding="utf-8"))
        if state.run_id != run_id:
            raise ValueError("checkpoint ID 不匹配")
        return state

    def save(self, state: RunSnapshot) -> None:
        atomic_json(self.path(state.run_id), state.model_dump())

    def list(self, metadata: dict[str, str] | None = None) -> list[RunSnapshot]:
        states = []
        for path in sorted(self.directory.glob("*.json"), key=lambda p: p.stat().st_mtime):
            try:
                state = self.load(path.stem)
            except (OSError, ValueError):
                continue
            if all(state.metadata.get(k) == v for k, v in (metadata or {}).items()):
                states.append(state)
        return states

    @contextmanager
    def lease(self, run_id: str):
        """OS lock is released on process death; retain the lock file to avoid races."""
        path = self.path(run_id).with_suffix(".lock")
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a+b") as stream:
            stream.seek(0, os.SEEK_END)
            if stream.tell() == 0:
                stream.write(b"0")
                stream.flush()
            stream.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise RuntimeError("该 checkpoint 正在被另一个执行器使用") from exc
            try:
                yield
            finally:
                stream.seek(0)
                if os.name == "nt":
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


class ToolJournal:
    def __init__(self, store: CheckpointStore, state: RunSnapshot):
        self.store, self.state = store, state

    def save(self):
        self.store.save(self.state)

    def set_plan(self, plan: BatchPlan):
        self.state.plan = plan
        self.save()

    def start(self, call: ToolCall):
        if call.id not in self.state.started:
            self.state.started.append(call.id)
        # Persist intent BEFORE invoking the tool; a failed save must stop execution.
        self.save()

    def finish(self, result: ToolResult):
        self.state.results[result.tool_call_id] = result
        if result.name == "deliver_file" and not tool_failed(result.content):
            calls = self.state.messages[-1].tool_calls or []
            call = next(c for c in calls if c.id == result.tool_call_id)
            path = (
                Path(self.state.workspace) / json.loads(call.function.arguments)["path"]
            ).resolve()
            if str(path) not in self.state.deliveries:
                self.state.deliveries.append(str(path))
        self.save()

    def recover(self, calls, registry, resolutions: dict[str, str] | None = None):
        by_id = {call.id: call for call in calls}
        resolutions = resolutions or {}
        uncertain = set(self.state.started) - self.state.results.keys()
        if resolutions.keys() - uncertain:
            raise ValueError("只能为结果不确定的调用提供核对结果")
        blocked = []
        for call_id in uncertain:
            call = by_id.get(call_id)
            if call is None:
                raise ValueError("checkpoint 工具调用记录不完整")
            tool = registry.get(call.function.name)
            if call_id in resolutions:
                self.finish(
                    ToolResult(
                        tool_call_id=call_id, name=call.function.name, content=resolutions[call_id]
                    )
                )
            elif not (tool and tool.supports_parallel):
                blocked.append(f"{call_id} ({call.function.name})")
        if blocked:
            raise UncertainToolExecutionError(
                "调用结果不确定，已暂停恢复，未重放有副作用的工具："
                + ", ".join(sorted(blocked))
                + "。请核对文件/外部系统后，通过 resume(resolutions={调用ID: 实际结果}) 提供结果。"
            )


def new_run_id() -> str:
    return uuid4().hex
