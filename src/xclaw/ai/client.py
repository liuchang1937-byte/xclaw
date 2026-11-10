"""
AI 客户端 - AI 交互的统一入口

职责：
- 提供统一的 LLM 调用接口
- 支持重试机制
- 管理 System Prompt
- 日志记录

对应 pi-mono 中 pi-ai 的核心客户端。
"""

from __future__ import annotations

import json
from contextlib import aclosing
from pathlib import Path
from typing import AsyncIterator

from loguru import logger

from xclaw.ai.context import (
    ContextBudgetExceededError,
    ContextCompressor,
    estimate_tokens,
    is_summary,
    protocol_boundaries,
    truncate_tool_content,
)
from xclaw.ai.provider import BaseProvider
from xclaw.ai.streaming import StreamAccumulator
from xclaw.ai.tool_protocol import InvalidToolCallResponseError, normalize_tool_response
from xclaw.ai.types import (
    ChatMessage,
    ChatResponse,
    StreamChunk,
    ToolDefinition,
    ToolResult,
)
from xclaw.persistence import atomic_json
from xclaw.telemetry import emit, observed_chat, observed_stream


class AIClient:
    """AI 客户端 - Agent 与大模型交互的统一入口

    职责：
    1. 封装 LLM 调用细节
    2. 管理对话历史
    3. 支持工具调用循环
    4. 支持流式和非流式两种模式

    使用示例：
        client = AIClient(provider, system_prompt="你是一个友好的助手")
        response = await client.chat([ChatMessage(role="user", content="你好")])
    """

    # 最大重试次数
    MAX_RETRIES = 3

    def __init__(
        self,
        provider: BaseProvider,
        *,
        system_prompt: str = "",
        model: str = "gpt-4o",
        temperature: float = 0.7,
        max_tokens: int = 4096,
    ):
        """
        初始化 AI 客户端

        Args:
            provider: LLM Provider 实例
            system_prompt: 系统提示词（定义 Agent 的行为和角色）
            model: 使用的模型名称
            temperature: 生成温度 (0-2)，越高越随机
            max_tokens: 最大生成 token 数
        """
        self.provider = provider
        self.system_prompt = system_prompt
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens

        # 对话历史
        self._history: list[ChatMessage] = []

        # Token 统计（累计）
        self.total_tokens = 0
        self.total_calls = 0

        # 上下文压缩器（可选，通过 enable_compression 启用）
        self.compressor: ContextCompressor | None = None
        self.context_strategy = "summary"

    def _build_messages(self, user_messages: list[ChatMessage]) -> list[ChatMessage]:
        """构建完整的消息列表（系统提示 + 历史 + 新消息）"""
        messages: list[ChatMessage] = []

        # 1. 系统提示词放在最前面
        if self.system_prompt:
            messages.append(ChatMessage(role="system", content=self.system_prompt))

        # 2. 历史对话
        messages.extend(self._history)

        # 3. 当前轮次的消息
        messages.extend(user_messages)

        return messages

    async def chat(
        self,
        messages: list[ChatMessage],
        tools: list[ToolDefinition] | None = None,
        *,
        save_history: bool = True,
    ) -> ChatResponse:
        """
        发送消息并获取完整响应（非流式）

        Args:
            messages: 用户消息列表
            tools: 可用工具列表
            save_history: 是否保存到对话历史

        Returns:
            ChatResponse: 完整响应
        """
        full_messages = await self._prepare_messages(messages, tools)

        # 重试逻辑
        for attempt in range(self.MAX_RETRIES):
            try:
                logger.debug(
                    f"LLM 请求: model={self.model}, "
                    f"messages_count={len(full_messages)}, "
                    f"tools_count={len(tools) if tools else 0}"
                )

                response = await observed_chat(
                    self.provider,
                    full_messages,
                    tools,
                    model=self.model,
                    temperature=self.temperature,
                    max_tokens=self.max_tokens,
                )
                response = normalize_tool_response(response, tools)

                # 记录 Token 用量 + 累计统计
                self.total_calls += 1
                if response.usage:
                    self.total_tokens += response.usage.total_tokens
                    logger.info(
                        f"LLM 响应: tokens={response.usage.total_tokens}, "
                        f"finish={response.finish_reason}"
                    )

                # 保存到历史
                if save_history:
                    self._history.extend(messages)
                    # 构建助手的回复消息
                    assistant_msg = ChatMessage(
                        role="assistant",
                        content=response.content,
                        tool_calls=response.tool_calls if response.tool_calls else None,
                        reasoning_content=response.reasoning_content,
                    )
                    self._history.append(assistant_msg)

                return response

            except InvalidToolCallResponseError:
                if attempt == self.MAX_RETRIES - 1:
                    raise InvalidToolCallResponseError(
                        "模型连续返回异常工具格式，未执行无效调用，请重试",
                    )
                logger.warning("模型返回异常工具协议，纠正后重试")
                correction = ChatMessage(
                    role="user",
                    content="请通过 API 的标准 tool_calls 字段调用已提供的工具，"
                    "不要在回复正文中输出 DSML、工具标记或调用示例。",
                )
                full_messages = await self._prepare_messages(messages + [correction], tools)
            except Exception as e:
                if getattr(e, "status_code", None) in {400, 401, 402, 403, 404, 405, 410, 422}:
                    emit("model.permanent_error", http_status=e.status_code)
                    raise
                logger.warning(f"LLM 调用失败 (第 {attempt + 1}/{self.MAX_RETRIES} 次): {e}")
                if attempt == self.MAX_RETRIES - 1:
                    raise
                # 指数退避等待
                import asyncio

                await asyncio.sleep(2**attempt)

        # 这里不会到达，但为了类型检查
        raise RuntimeError("LLM 调用失败，已达最大重试次数")

    async def chat_stream(
        self,
        messages: list[ChatMessage],
        tools: list[ToolDefinition] | None = None,
        *,
        save_history: bool = True,
    ) -> AsyncIterator[StreamChunk]:
        """
        发送消息并获取流式响应

        Args:
            messages: 用户消息列表
            tools: 可用工具列表
            save_history: 是否保存到对话历史

        Yields:
            StreamChunk: 流式输出的片段
        """
        # DeepSeek 的控制标记可能跨片段出现，先取得完整响应再交付，避免泄露协议文本。
        if self.model.startswith("deepseek"):
            response = await self.chat(messages, tools, save_history=save_history)
            yield StreamChunk(
                delta_content=response.content,
                delta_tool_calls=response.tool_calls or None,
                delta_reasoning_content=response.reasoning_content,
                finish_reason=response.finish_reason,
                usage=response.usage,
            )
            return
        full_messages = await self._prepare_messages(messages, tools)

        accumulator = StreamAccumulator()

        async with aclosing(
            observed_stream(
                self.provider,
                full_messages,
                tools,
                model=self.model,
                temperature=self.temperature,
                max_tokens=self.max_tokens,
            )
        ) as stream:
            async for chunk in stream:
                accumulator.add(chunk)
                yield chunk

        self.total_calls += 1
        if accumulator.usage:
            self.total_tokens += accumulator.usage.total_tokens

        # 保存到历史
        if save_history:
            response = accumulator.response()
            assistant_msg = ChatMessage(
                role="assistant",
                content=response.content,
                tool_calls=response.tool_calls or None,
                reasoning_content=response.reasoning_content,
            )
            self._history.extend(messages)
            self._history.append(assistant_msg)

    def add_tool_results(self, results: list[ToolResult]) -> None:
        """将工具执行结果添加到对话历史

        Args:
            results: 工具执行结果列表
        """
        for result in results:
            self._history.append(
                ChatMessage(
                    role="tool",
                    content=result.content,
                    tool_call_id=result.tool_call_id,
                    name=result.name,
                )
            )

    def clear_history(self) -> None:
        """清空对话历史"""
        self._history.clear()
        logger.info("对话历史已清空")

    async def _prepare_messages(
        self,
        messages: list[ChatMessage],
        tools: list[ToolDefinition] | None,
    ) -> list[ChatMessage]:
        """在完整请求通过预算检查后才提交压缩结果，失败时保留原始消息。"""
        if not self.compressor:
            return self._build_messages(messages)
        compressor = self.compressor
        if compressor.max_output_tokens < self.max_tokens:
            raise ValueError("上下文输出预留小于实际 max_tokens，请重新配置压缩器")
        protocol_boundaries(self._history + messages)

        def size(history, current):
            return compressor.estimate_request_tokens(
                history,
                current,
                system_prompt=self.system_prompt,
                tools=tools,
            )

        budget = compressor.input_budget
        before = size(self._history, messages)
        if before <= budget:
            return self._build_messages(messages)
        protected = [
            message
            for index, message in enumerate(messages)
            if message.role in ("system", "user") and (index == 0 or not is_summary(message))
        ]
        fixed_size = size([], protected)
        if fixed_size > budget:
            emit(
                "context.blocked", estimated_tokens=fixed_size, budget=budget, reason="fixed_input"
            )
            raise ContextBudgetExceededError(
                fixed_size,
                budget,
                "当前用户输入、系统提示或工具定义本身过大。",
            )

        history, current = list(self._history), list(messages)
        old_summary = compressor.summary
        old_calls = compressor.summary_calls
        summary_failed = False
        reason = ""
        summary_tokens = min(1024, max(1, budget // 4))
        try:
            # 最近消息数量是偏好；预算紧张时逐步减少，但始终按整轮切割。
            keeps = dict.fromkeys(
                (compressor.keep_recent, max(1, compressor.keep_recent // 2), 1, 0)
            )
            for keep in keeps:
                if size(history, current) <= budget:
                    break
                if self.context_strategy == "truncate":
                    # Baseline removes complete old turns instead of calling a summary model.
                    cut = compressor._find_cut_point(history, keep_recent=keep)
                    history = history[cut:]
                    continue
                history = await compressor.compress(
                    history,
                    keep_recent=keep,
                    summary_tokens=summary_tokens,
                    max_calls=max(
                        0, compressor.MAX_SUMMARY_CALLS - compressor.summary_calls + old_calls
                    ),
                )
                if compressor.last_failure:
                    summary_failed = True
                    reason = compressor.last_failure
                    break

            # 本轮原始用户输入固定保留，只总结完整的旧工具调用组。
            can_summarize_current = (
                current
                and current[0].role == "user"
                and all(message.role != "user" or is_summary(message) for message in current[1:])
            )
            if can_summarize_current and not summary_failed:
                for keep in dict.fromkeys((compressor.keep_recent, 1)):
                    if size(history, current) <= budget:
                        break
                    if self.context_strategy == "truncate":
                        cut = compressor._find_cut_point(
                            current[1:], keep_recent=keep, whole_turns=False
                        )
                        current = current[:1] + current[1 + cut :]
                        continue
                    current = await compressor.compress(
                        current,
                        keep_recent=keep,
                        preserve_first=True,
                        summary_tokens=summary_tokens,
                        max_calls=max(
                            0, compressor.MAX_SUMMARY_CALLS - compressor.summary_calls + old_calls
                        ),
                    )
                    if compressor.last_failure:
                        reason = compressor.last_failure
                        break

            # 超长的最新工具结果无法靠切掉旧消息解决；有标记地保留首尾。
            candidates = sorted(
                (index for index, message in enumerate(current) if message.role == "tool"),
                key=lambda index: estimate_tokens(current[index].content),
                reverse=True,
            )
            for index in candidates:
                excess = size(history, current) - budget
                if excess <= 0:
                    break
                message = current[index]
                content = message.content or ""
                clipped = truncate_tool_content(content, max(0, estimate_tokens(content) - excess))
                if estimate_tokens(clipped) < estimate_tokens(content):
                    current[index] = message.model_copy(update={"content": clipped})

            after = size(history, current)
            if after > budget:
                raise ContextBudgetExceededError(after, budget, reason)
            protocol_boundaries(history + current)
        except BaseException as exc:
            compressor.summary = old_summary
            emit(
                "context.blocked",
                estimated_tokens=size(history, current),
                budget=budget,
                reason=type(exc).__name__,
            )
            raise
        self._history = history
        messages[:] = current
        emit(
            "context.prepared",
            before_estimated_tokens=before,
            after_estimated_tokens=after,
            budget=budget,
            strategy=self.context_strategy,
            summary_calls=compressor.summary_calls - old_calls,
            summary_failure=compressor.last_failure or None,
        )
        logger.info(
            f"上下文请求预算: {before} → {after} tokens, budget={budget}, "
            f"summary_calls={compressor.summary_calls - old_calls}"
        )
        return self._build_messages(messages)

    def enable_compression(
        self,
        *,
        context_window: int = 32768,
        max_output_tokens: int | None = None,
        trigger_ratio: float = 0.85,
        safety_margin: int = 1024,
        keep_recent: int = 6,
    ) -> None:
        """启用上下文压缩

        Args:
            context_window: 模型上下文上限（输入+输出）
            max_output_tokens: 输出预留；默认使用 AIClient.max_tokens
            trigger_ratio: 输入预算触发比例
            safety_margin: tokenizer 误差和协议安全余量
            keep_recent: 优先保留最近 N 条消息；超预算时按完整轮次减少
        """
        reserved = max_output_tokens if max_output_tokens is not None else self.max_tokens
        if reserved < self.max_tokens:
            raise ValueError("输出预留不能小于 AIClient.max_tokens")
        self.compressor = ContextCompressor(
            self.provider,
            model=self.model,
            context_window=context_window,
            max_output_tokens=reserved,
            trigger_ratio=trigger_ratio,
            safety_margin=safety_margin,
            keep_recent=keep_recent,
        )
        logger.info(
            f"上下文压缩已启用 (window={context_window}, "
            f"input_budget={self.compressor.input_budget})"
        )

    def save_history(self, path: str) -> None:
        """把已保存的对话历史持久化到 JSON；执行中任务由 Checkpoint 恢复。

        Args:
            path: 保存路径
        """
        data = [m.model_dump() for m in self._history]
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        atomic_json(p, data)
        logger.info(f"对话历史已保存: {path} ({len(data)} 条)")

    def load_history(self, path: str) -> None:
        """从 JSON 文件恢复对话历史

        Args:
            path: 历史文件路径
        """
        p = Path(path)
        if not p.exists():
            logger.warning(f"历史文件不存在: {path}")
            return
        data = json.loads(p.read_text(encoding="utf-8"))
        self._history = [ChatMessage.model_validate(d) for d in data]
        logger.info(f"对话历史已加载: {len(self._history)} 条")

    def get_token_stats(self) -> dict[str, int]:
        """获取累计 Token 统计

        Returns:
            dict: {"total_calls": 调用次数, "total_tokens": 累计 token}
        """
        return {
            "total_calls": self.total_calls,
            "total_tokens": self.total_tokens,
        }

    def get_history(self) -> list[ChatMessage]:
        """获取当前对话历史（只读副本）"""
        return list(self._history)

    def set_history(self, messages: list[ChatMessage]) -> None:
        """设置对话历史（用于恢复之前的对话）

        Args:
            messages: 要恢复的对话消息列表
        """
        self._history = list(messages)
        logger.info(f"对话历史已恢复: {len(self._history)} 条消息")
