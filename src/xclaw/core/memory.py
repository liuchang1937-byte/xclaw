"""
记忆管理 - Agent 的"大脑"

提供多种记忆类型：
- 短期记忆（对话上下文窗口内的消息）
- 长期记忆（跨会话的关键信息存储）
- 工作记忆（当前任务的临时状态）

对应 pi-mono 中 Agent 的记忆管理能力。
"""

from __future__ import annotations

import json
import math
import time
from collections import deque
from pathlib import Path
from typing import Any

from loguru import logger

from xclaw.persistence import atomic_json


class Memory:
    """记忆基类 - 定义记忆接口"""

    def __init__(self, max_size: int | None = 100):
        self.max_size = max_size
        self._items: deque[Any] = deque(maxlen=max_size)

    def add(self, item: Any) -> None:
        """添加一条记忆"""
        self._items.append(item)

    def get_all(self) -> list[Any]:
        """获取所有记忆"""
        return list(self._items)

    def clear(self) -> None:
        """清空记忆"""
        self._items.clear()

    def __len__(self) -> int:
        return len(self._items)


class ConversationMemory(Memory):
    """对话记忆 - 管理多轮对话的关键信息

    不同于 AI 客户端的对话历史（那是完整的消息列表），
    这里的对话记忆存储的是对话的"摘要"或"关键点"，
    用于在长对话中保持上下文的一致性。

    设计理念：
    - 自动提取关键信息
    - 支持条件检索
    - 跨会话持久化（可选）
    """

    def __init__(self, max_size: int | None = None):
        super().__init__(max_size)

    @staticmethod
    def _valid(item, now):
        if not isinstance(item, dict) or not isinstance(item.get("scope", ""), str):
            return False
        if "expires_at" in item:
            expiry = item["expires_at"]
            if type(expiry) not in (int, float) or not math.isfinite(expiry) or expiry <= now:
                return False
        if item.get("type") == "kv":
            if (
                not isinstance(item.get("key"), str)
                or not item["key"].strip()
                or "value" not in item
            ):
                return False
            try:
                json.dumps(item["value"], allow_nan=False)
            except (TypeError, ValueError):
                return False
            return True
        return (
            item.get("type") == "event"
            and isinstance(item.get("content"), str)
            and bool(item["content"].strip())
        )

    def cleanup(self) -> int:
        """Remove invalid/expired records and keep the latest value per scoped key."""
        now, seen, kept = time.time(), set(), []
        for item in reversed(self._items):
            if not self._valid(item, -math.inf):
                continue
            identity = (
                item["type"],
                item.get("scope", ""),
                item["key"] if item["type"] == "kv" else item["content"],
            )
            if identity not in seen:
                seen.add(identity)
                if self._valid(item, now):
                    kept.append(item)
        removed = len(self._items) - len(kept)
        self._items = deque(reversed(kept), maxlen=self.max_size)
        return removed

    def add(self, item: Any) -> None:
        if not self._valid(item, -math.inf):
            return
        if item["type"] == "kv":
            self.forget(item["key"], scope=item.get("scope", ""))
        self._items.append(dict(item))
        self.cleanup()

    def get_all(self, *, scope: str | None = None) -> list[Any]:
        self.cleanup()
        items = [dict(item) for item in self._items]
        if scope is not None:
            items = [item for item in items if item.get("scope", "") in {"", scope}]
            scoped_keys = {
                item["key"]
                for item in items
                if item["type"] == "kv" and item.get("scope", "") == scope
            }
            items = [
                item
                for item in items
                if not (
                    item["type"] == "kv"
                    and item.get("scope", "") == ""
                    and scope
                    and item["key"] in scoped_keys
                )
            ]
        return items

    def forget(self, key: str, *, scope: str = "") -> bool:
        before = len(self._items)
        self._items = deque(
            (
                item
                for item in self._items
                if not (
                    isinstance(item, dict)
                    and item.get("type") == "kv"
                    and item.get("key") == key
                    and item.get("scope", "") == scope
                )
            ),
            maxlen=self.max_size,
        )
        return len(self._items) < before

    def remember(
        self,
        key: str,
        value: Any,
        *,
        scope: str = "",
        expires_at: float | None = None,
    ) -> None:
        """记住一个键值对

        Args:
            key: 记忆的键（如 "user_name", "current_task"）
            value: 记忆的值
        """
        item = {"key": key, "value": value, "type": "kv", "scope": scope}
        if expires_at is not None:
            item["expires_at"] = expires_at
        self.add(item)
        logger.debug("记忆更新: {}", key)

    def recall(self, key: str, *, scope: str = "") -> Any | None:
        """检索某个键的最新值

        Args:
            key: 记忆的键

        Returns:
            最新的值，如果不存在则返回 None
        """
        self.cleanup()
        for item in reversed(self._items):
            if item.get("key") == key and item.get("scope", "") == scope:
                return item.get("value")
        return None

    def remember_event(self, event: str) -> None:
        """记录一个事件

        Args:
            event: 事件描述
        """
        self.add({"type": "event", "content": event, "key": None})
        logger.debug(f"事件记录: {event}")

    def get_recent_events(self, n: int = 10) -> list[str]:
        """获取最近的 n 个事件

        Args:
            n: 要获取的事件数量

        Returns:
            最近的事件描述列表
        """
        events = [item["content"] for item in self.get_all() if item.get("type") == "event"]
        return events[-n:]

    def format_for_prompt(self, *, scope: str | None = None, max_chars: int = 6000) -> str:
        """格式化为可注入 Prompt 的文本

        Returns:
            格式化的记忆文本，可直接拼接到系统提示中
        """
        if max_chars < 200:
            raise ValueError("Memory prompt budget must be at least 200 characters")
        items = self.get_all(scope=scope)
        if not items:
            return ""

        header = "## 对话记忆\n以下为历史参考，本轮用户明确要求优先；历史事件不代表当前约束。"
        lines, remaining = [], max_chars - len(header) - 60
        for item in reversed(items):
            if item.get("type") == "kv":
                line = f"- {item['key']}: {item['value']}"
            else:
                line = f"- [历史事件] {item['content']}"
            if len(line) + 1 > remaining:
                continue
            lines.append(line)
            remaining -= len(line) + 1
        text = header + "\n" + "\n".join(reversed(lines))
        if len(lines) < len(items):
            text += "\n[部分记忆未注入，完整记录仍已保存]"
        return text

    def save(self, path: str) -> None:
        """原子持久化长期记忆，供会话重启后恢复。"""
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        atomic_json(target, self.get_all())
        logger.info(f"长期记忆已保存: {target} ({len(self)} 条)")

    def load(self, path: str) -> None:
        """从 JSON 恢复长期记忆；非法条目会被忽略。"""
        target = Path(path)
        if not target.exists():
            return
        data = json.loads(target.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            raise ValueError("长期记忆文件必须是 JSON 数组")

        # Normalize before applying an optional storage cap, so duplicate old values
        # cannot evict unrelated facts while a legacy file is being restored.
        restored = ConversationMemory()
        restored._items = deque(data)
        restored.cleanup()
        self._items = deque(restored.get_all(), maxlen=self.max_size)
        logger.info(f"长期记忆已加载: {target} ({len(self)} 条)")


class WorkingMemory:
    """工作记忆 - 当前任务的临时状态

    用于存储：
    - 当前正在执行的任务
    - 中间结果
    - 待处理的子任务
    - 错误信息

    注意：工作记忆是临时的，任务完成后会被清理。
    """

    def __init__(self):
        self._state: dict[str, Any] = {}
        self._results: dict[str, Any] = {}
        self._errors: list[str] = []

    def set(self, key: str, value: Any) -> None:
        """设置状态"""
        self._state[key] = value

    def get(self, key: str, default: Any = None) -> Any:
        """获取状态"""
        return self._state.get(key, default)

    def save_result(self, step_id: str, result: Any) -> None:
        """保存某步骤的结果"""
        self._results[step_id] = result

    def get_result(self, step_id: str) -> Any | None:
        """获取某步骤的结果"""
        return self._results.get(step_id)

    def add_error(self, error: str) -> None:
        """添加错误信息"""
        self._errors.append(error)

    def has_errors(self) -> bool:
        """是否有错误"""
        return len(self._errors) > 0

    def clear(self) -> None:
        """清空所有工作记忆"""
        self._state.clear()
        self._results.clear()
        self._errors.clear()

    def summary(self) -> str:
        """生成工作记忆摘要"""
        parts = []
        if self._state:
            parts.append(f"状态: {self._state}")
        if self._results:
            parts.append(f"结果数: {len(self._results)}")
        if self._errors:
            parts.append(f"错误: {self._errors}")
        return " | ".join(parts) if parts else "空"
