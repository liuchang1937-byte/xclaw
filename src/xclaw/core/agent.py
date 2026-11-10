"""
Agent 基类 - 所有 Agent 的抽象基础

设计理念：
- 最小化核心：Agent 基类只定义最基础的循环逻辑
- 组合优于继承：通过组合 Tool、Memory、Planner 来构建具体 Agent
- 透明可观测：每个关键步骤都有日志输出

对应 pi-mono 中 pi-agent-core 的 Agent 核心实现。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from abc import ABC, abstractmethod
from contextlib import ExitStack, aclosing
from pathlib import Path
from typing import Any, Callable

from loguru import logger

from xclaw.ai.client import AIClient
from xclaw.ai.provider import BaseProvider
from xclaw.ai.streaming import StreamAccumulator
from xclaw.ai.types import (
    ChatMessage,
    ChatResponse,
    ToolCall,
    ToolDefinition,
    ToolResult,
)
from xclaw.core.checkpoint import CheckpointStore, RunSnapshot, ToolJournal, new_run_id
from xclaw.core.memory import ConversationMemory, WorkingMemory
from xclaw.core.memory_extractor import MemoryExtractor
from xclaw.core.planner import Planner, StepStatus
from xclaw.core.scheduling import SchedulingConfig, ToolScheduler
from xclaw.core.tool import Tool, ToolRegistry
from xclaw.core.tool import tool_registry as global_registry
from xclaw.mom.bus import Event, EventBus
from xclaw.telemetry import RunTrace, activate, emit


class Agent(ABC):
    """Agent 基类

    定义了 Agent 的核心执行循环（ReAct 模式）：
    1. Think  → LLM 分析当前状态，决定下一步行动
    2. Act    → 执行工具调用
    3. Observe → 收集工具执行结果
    4. Repeat  → 循环直到任务完成

    子类需要实现：
    - system_prompt: 定义 Agent 的角色和行为
    - 可选的特定工具和逻辑

    使用示例：
        class MyAgent(Agent):
            @property
            def system_prompt(self) -> str:
                return "你是一个友好的助手..."

        agent = MyAgent(provider, model="gpt-4o")
        response = await agent.run("帮我搜索最新的 AI 新闻")
    """

    # 最大工具调用轮次（防止无限循环）
    MAX_TOOL_ROUNDS = 10

    def __init__(
        self,
        provider: BaseProvider,
        *,
        model: str = "gpt-4o",
        temperature: float = 0.7,
        max_tokens: int = 4096,
        tool_registry: ToolRegistry | None = None,
        event_bus: EventBus | None = None,
        auto_memory: bool = False,
        tool_scheduling: SchedulingConfig | None = None,
        checkpoint_dir: str | None = None,
    ):
        """
        初始化 Agent

        Args:
            provider: LLM Provider
            model: 模型名称
            temperature: 生成温度
            max_tokens: 最大 token 数
            tool_registry: 工具注册中心（默认使用全局实例）
        """
        # 空的 ToolRegistry 因实现了 __len__ 会被判定为 False，不能使用
        # ``tool_registry or global_registry``，否则 Agent 私有注册中心会意外
        # 回退到全局单例，破坏会话之间的 Sandbox 隔离。
        self.tool_registry = tool_registry if tool_registry is not None else global_registry

        # 创建 AI 客户端
        self.client = AIClient(
            provider=provider,
            system_prompt=self.system_prompt,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        self.tool_scheduler = ToolScheduler(self.client, self.tool_registry, tool_scheduling)
        self.client.system_prompt += (
            "\n工具执行规则：独立调用可以在同一轮提交；依赖前置结果才能确定参数的调用，"
            "必须等前置结果返回后在下一轮提交，不得猜测结果或使用占位符。"
        )

        # 记忆系统
        self.conversation_memory = ConversationMemory()
        self.working_memory = WorkingMemory()
        self.memory_scope = "project:default"

        # 规划器
        self.planner = Planner()

        # 事件总线（可选，用于订阅生命周期事件）
        self.event_bus = event_bus

        # 在执行前维护用户事实，避免旧约束影响本轮任务。
        self.auto_memory = auto_memory
        self.memory_extractor = (
            MemoryExtractor(self.client.provider, model=self.client.model) if auto_memory else None
        )

        # 运行时状态
        self._is_running = False
        self._run_active = False
        self.trace_dir: str | None = None
        self.trace_capture_content = False
        self.last_trace: RunTrace | None = None
        self.last_run_status = "idle"
        self.run_metadata: dict[str, str] = {}
        self.checkpoint_store = CheckpointStore(checkpoint_dir) if checkpoint_dir else None
        self.last_checkpoint_id: str | None = None
        self._journal: ToolJournal | None = None
        self._resuming = False

    # ─── 子类必须实现的属性 ─────────────────────────────

    @property
    @abstractmethod
    def system_prompt(self) -> str:
        """系统提示词 - 定义 Agent 的角色和行为

        子类必须实现此属性。
        示例：
            return "你是一个专业的编程助手，可以读写文件、执行代码..."
        """
        ...

    @property
    def agent_name(self) -> str:
        """Agent 名称（用于日志标识）"""
        return self.__class__.__name__

    # ─── 核心执行循环 ──────────────────────────────────

    async def run(
        self,
        user_input: str,
        *,
        tools: list[str] | None = None,
        stream_callback=None,
        completion_validator: Callable[[str], str | None] | None = None,
        resume_from: str | None = None,
        resolutions: dict[str, str] | None = None,
    ) -> str:
        if self._run_active:
            raise RuntimeError("同一 Agent 不能并发执行任务")
        trace = RunTrace(self.trace_dir, capture_content=self.trace_capture_content)
        self.last_trace = trace
        self.last_run_status = "running"
        self._run_active = True
        started = time.perf_counter()
        with ExitStack() as stack, activate(trace):
            try:
                self._journal = None
                self.last_checkpoint_id = None
                self._resuming = resume_from is not None
                if resume_from and not self.checkpoint_store:
                    raise ValueError("恢复任务需要配置 checkpoint_dir")
                if self.checkpoint_store:
                    checkpoint_id = resume_from or new_run_id()
                    stack.enter_context(self.checkpoint_store.lease(checkpoint_id))
                    if resume_from:
                        state = self.checkpoint_store.load(resume_from)
                        self._validate_snapshot(state, completion_validator)
                        self.client.set_history(state.history)
                        self.client.total_calls = state.total_calls
                        self.client.total_tokens = state.total_tokens
                        self.conversation_memory = ConversationMemory()
                        for item in state.memory:
                            self.conversation_memory.add(item)
                        self.memory_scope = state.memory_scope
                        if self.client.compressor:
                            self.client.compressor.summary = state.summary
                        user_input, tools = state.user_input, state.tools
                    else:
                        state = RunSnapshot(
                            run_id=checkpoint_id,
                            workspace=self._checkpoint_workspace(),
                            fingerprint=self._checkpoint_fingerprint(),
                            user_input=user_input,
                            metadata=dict(self.run_metadata),
                            tools=tools,
                            completion_required=completion_validator is not None,
                        )
                    self._journal = ToolJournal(self.checkpoint_store, state)
                    self.last_checkpoint_id = checkpoint_id
                    if resume_from and state.phase == "finished":
                        self.last_run_status = state.status
                        return state.latest_output
                    if resume_from and state.phase == "tools":
                        self._journal.recover(
                            state.messages[-1].tool_calls or [], self.tool_registry, resolutions
                        )
                    elif resolutions:
                        raise ValueError("当前 checkpoint 没有待核对的工具调用")
                    state.status, state.last_error = "running", ""
                emit(
                    "run.started",
                    model=self.client.model,
                    agent=self.agent_name,
                    prompt_hash=hashlib.sha256(self.client.system_prompt.encode()).hexdigest(),
                    tools_hash=hashlib.sha256(
                        json.dumps(
                            [d.model_dump() for d in self.tool_registry.get_all_definitions()],
                            sort_keys=True,
                            ensure_ascii=False,
                        ).encode()
                    ).hexdigest(),
                    session_id=self.run_metadata.get("session_id"),
                    message_id=self.run_metadata.get("message_id"),
                    checkpoint_id=self.last_checkpoint_id,
                    resumed=self._resuming,
                )
                result = await self._run(
                    user_input,
                    tools=tools,
                    stream_callback=stream_callback,
                    completion_validator=completion_validator,
                )
                if self.last_run_status == "running":
                    self.last_run_status = "completed"
                if self._journal:
                    self._journal.state.phase = "finished"
                    self._journal.state.status = self.last_run_status
                    self._journal.state.latest_output = result
                    self._capture_checkpoint()
                return result
            except BaseException as exc:
                self.last_run_status = (
                    "cancelled" if isinstance(exc, asyncio.CancelledError) else "error"
                )
                emit("run.error", error_type=type(exc).__name__)
                if self._journal:
                    try:
                        # Read the durable boundary: an earlier write may have
                        # failed after mutating the in-memory journal.
                        saved = self.checkpoint_store.load(self.last_checkpoint_id)
                        if saved.phase != "finished":
                            saved.status = "interrupted"
                            saved.last_error = type(exc).__name__
                            self.checkpoint_store.save(saved)
                    except (OSError, ValueError):
                        logger.warning("未能更新 checkpoint 中断状态，保留最近的有效快照")
                raise
            finally:
                self._journal = None
                self._is_running = False
                self._run_active = False
                emit(
                    "run.finished",
                    status=self.last_run_status,
                    duration_seconds=time.perf_counter() - started,
                    metrics=trace.metrics(),
                )

    async def resume(self, checkpoint_id: str, *, resolutions=None, **kwargs) -> str:
        """Continue a saved run; uncertain side effects require inspected results."""
        return await self.run("", resume_from=checkpoint_id, resolutions=resolutions, **kwargs)

    def _checkpoint_workspace(self) -> str:
        return str(Path(getattr(self, "workspace_dir", ".")).resolve())

    def _checkpoint_fingerprint(self) -> str:
        contract = {
            "model": self.client.model,
            "prompt": self.client.system_prompt,
            "provider": (
                type(self.client.provider).__module__,
                type(self.client.provider).__qualname__,
                str(getattr(self.client.provider, "base_url", "")),
            ),
            "tools": [
                (
                    d.model_dump(),
                    sorted(c.value for c in (t.capabilities or ())),
                    t.parallel_safe,
                    bool(t.request_builder),
                )
                for d in self.tool_registry.get_all_definitions()
                for t in [self.tool_registry.get(d.name)]
            ],
            "policy": repr(getattr(self, "execution_policy", None)),
            "docker_image": getattr(self, "docker_image", None),
            "rounds": self.MAX_TOOL_ROUNDS,
            "context_strategy": self.client.context_strategy,
            "max_tokens": self.client.max_tokens,
            "context_window": (
                self.client.compressor.context_window if self.client.compressor else None
            ),
        }
        return hashlib.sha256(json.dumps(contract, sort_keys=True).encode()).hexdigest()

    def _validate_snapshot(self, state, validator):
        if state.workspace != self._checkpoint_workspace() or not Path(state.workspace).is_dir():
            raise ValueError("checkpoint 工作区不匹配或已不存在")
        if state.fingerprint != self._checkpoint_fingerprint():
            raise ValueError("模型、提示词、工具或执行配置已改变，不能恢复此 checkpoint")
        for key in ("session_id", "source_workspace"):
            if state.metadata.get(key) != self.run_metadata.get(key):
                raise ValueError("checkpoint 不属于当前会话/项目")
        if state.completion_required != (validator is not None):
            raise ValueError("恢复时必须保持原任务的完成校验器")

    def _capture_checkpoint(self, messages=None):
        if not self._journal:
            return
        state = self._journal.state
        if messages is not None:
            state.messages = [m.model_copy(deep=True) for m in messages]
        state.history = self.client.get_history()
        state.memory = self.conversation_memory.get_all()
        state.memory_scope = self.memory_scope
        state.summary = self.client.compressor.summary if self.client.compressor else ""
        state.total_calls, state.total_tokens = self.client.total_calls, self.client.total_tokens
        self._journal.save()

    async def _run(
        self,
        user_input: str,
        *,
        tools: list[str] | None = None,
        stream_callback=None,
        completion_validator: Callable[[str], str | None] | None = None,
    ) -> str:
        """执行 Agent 主循环

        ReAct 循环：
        while 任务未完成:
            1. 调用 LLM，传递当前上下文和可用工具
            2. 如果 LLM 返回文本 → 任务完成，返回结果
            3. 如果 LLM 发起工具调用 → 执行工具，将结果反馈给 LLM
            4. 检查是否超过最大轮次

        Args:
            user_input: 用户输入的任务描述
            tools: 要使用的工具名称列表（None = 全部可用工具）
            stream_callback: 流式输出的回调函数 async callback(text_chunk)

        Returns:
            str: Agent 的最终回复
        """
        self._is_running = True
        self.working_memory.clear()

        logger.info(f"[{self.agent_name}] 开始执行任务: {user_input}")

        # 发射生命周期事件：agent.started
        if self.event_bus:
            await self.event_bus.emit(
                Event(
                    type="agent.started",
                    source=self.agent_name,
                    data={"input": user_input, "run_id": self.last_trace.run_id},
                )
            )

        # 获取可用工具
        available_tools = self._get_tool_definitions(tools)

        if self.auto_memory and not self._resuming:
            await self._extract_memories(user_input)

        # 构建初始消息
        messages = (
            list(self._journal.state.messages)
            if self._resuming
            else self._build_initial_messages(user_input)
        )

        # 工具调用轮次
        tool_round = self._journal.state.iteration if self._resuming else 0
        if self._journal:
            if self._journal.state.phase == "tools":
                await self._observe_tools(messages)
            else:
                self._capture_checkpoint(messages)

        while self._is_running and tool_round < self.MAX_TOOL_ROUNDS:
            tool_round += 1
            emit("run.round", round=tool_round)
            logger.info(f"[{self.agent_name}] 第 {tool_round} 轮思考...")

            # Step 1: Think - 调用 LLM
            if stream_callback and tool_round == 1:
                # 首轮支持流式输出
                response = await self._think_stream(messages, available_tools, stream_callback)
            else:
                response = await self._think(messages, available_tools)

            if self._journal:
                self._journal.state.latest_output = response.content or ""

            # Step 2: 判断 LLM 的响应类型
            if response.tool_calls:
                # 发起工具调用
                logger.info(
                    f"[{self.agent_name}] 调用工具: "
                    f"{[tc.function.name for tc in response.tool_calls]}"
                )

                # Step 4: Observe - 按 API 要求的顺序添加消息
                # 顺序必须是：assistant(tool_calls) → tool(result)
                # 注意：不调用 self.client.add_tool_results() 避免污染持久历史

                # 1. 先添加助手消息（包含 tool_calls）
                messages.append(
                    ChatMessage(
                        role="assistant",
                        content=None,
                        tool_calls=response.tool_calls,
                        reasoning_content=response.reasoning_content,
                    )
                )
                if self._journal:
                    self._journal.state.iteration = tool_round
                    self._journal.state.phase = "tools"
                    self._capture_checkpoint(messages)
                await self._observe_tools(messages)

            else:
                # 没有工具调用 → 任务完成
                final_response = response.content or ""
                correction = completion_validator(final_response) if completion_validator else None
                if correction:
                    messages.extend(
                        [
                            ChatMessage(
                                role="assistant",
                                content=final_response,
                                reasoning_content=response.reasoning_content,
                            ),
                            ChatMessage(role="user", content=correction),
                        ]
                    )
                    if self._journal:
                        self._journal.state.iteration = tool_round
                        self._journal.state.latest_output = final_response
                        self._capture_checkpoint(messages)
                    continue
                logger.info(f"[{self.agent_name}] 任务完成: {final_response[:100]}...")
                self._is_running = False

                # 保存本轮对话到持久历史（用户输入 + 助手回复）
                # 不包含中间的工具调用链，避免污染下次 run()
                self.client._history.append(ChatMessage(role="user", content=user_input))
                self.client._history.append(
                    ChatMessage(
                        role="assistant",
                        content=final_response,
                        reasoning_content=response.reasoning_content,
                    )
                )
                if self._journal:
                    self._journal.state.iteration = tool_round
                    self._journal.state.latest_output = final_response
                    # Persist terminal state before callbacks that may fail.
                    self._journal.state.phase = "finished"
                    self._journal.state.status = "completed"
                    self._capture_checkpoint(messages)

                # 发射生命周期事件：agent.completed
                if self.event_bus:
                    await self.event_bus.emit(
                        Event(
                            type="agent.completed",
                            source=self.agent_name,
                            data={"output": final_response[:200], "run_id": self.last_trace.run_id},
                        )
                    )

                return final_response

        # 超过最大轮次
        logger.warning(f"[{self.agent_name}] 达到最大工具调用轮次，强制结束")
        self._is_running = False

        self.last_run_status = "round_limit" if tool_round >= self.MAX_TOOL_ROUNDS else "stopped"

        if completion_validator and completion_validator(""):
            return "任务未完成：尚未生成可发送的最终文件，请重试。"

        # 让 LLM 总结当前进度
        summary = await self._think(messages, None)
        return summary.content or "任务已超时，请简化任务描述后重试。"

    async def _observe_tools(self, messages):
        calls = messages[-1].tool_calls or []
        # The scheduling model sees the same context as before journaling.
        results = await self._act(calls, messages[:-1])
        for result in results:
            messages.append(
                ChatMessage(
                    role="tool",
                    content=result.content,
                    tool_call_id=result.tool_call_id,
                    name=result.name,
                )
            )
        if self._journal:
            state = self._journal.state
            state.phase = "ready"
            state.plan, state.started, state.results = None, [], {}
            self._capture_checkpoint(messages)

    async def run_with_plan(
        self,
        user_input: str,
        steps: list[dict[str, Any]],
    ) -> str:
        """按照预设计划执行任务

        Args:
            user_input: 用户输入
            steps: 步骤列表，每个步骤包含:
                - id: 步骤 ID
                - description: 描述
                - tool_name: 工具名（可选）
                - tool_args: 工具参数（可选）
                - depends_on: 依赖（可选）

        Returns:
            str: 执行总结
        """
        task_id = f"task_{id(user_input)}"
        self.planner.create_plan(task_id, user_input)

        for step_def in steps:
            self.planner.add_step(
                task_id=task_id,
                step_id=step_def["id"],
                description=step_def["description"],
                tool_name=step_def.get("tool_name"),
                tool_args=step_def.get("tool_args", {}),
                depends_on=step_def.get("depends_on", []),
            )

        # 按顺序执行步骤
        results = []
        while not self.planner.is_completed(task_id):
            step = self.planner.get_next_step(task_id)
            if step is None:
                break

            self.planner.update_step(task_id, step.id, status=StepStatus.RUNNING)

            try:
                if step.tool_name:
                    result = await self.tool_registry.execute(
                        step.tool_name, json.dumps(step.tool_args)
                    )
                else:
                    # 没有指定工具，让 LLM 处理
                    result = await self.run(step.description)

                self.planner.update_step(
                    task_id,
                    step.id,
                    status=StepStatus.COMPLETED,
                    result=result,
                )
                results.append(f"✅ {step.id}: {step.description}\n  结果: {result}")

            except Exception as e:
                self.planner.update_step(
                    task_id,
                    step.id,
                    status=StepStatus.FAILED,
                    error=str(e),
                )
                results.append(f"❌ {step.id}: {step.description}\n  错误: {e}")

        return "\n\n".join(results)

    # ─── 内部方法 ──────────────────────────────────────

    def _build_initial_messages(self, user_input: str) -> list[ChatMessage]:
        """构建初始消息列表

        将用户输入、记忆上下文等组装成消息列表。
        """
        messages: list[ChatMessage] = []

        # 注入对话记忆
        memory_text = self.conversation_memory.format_for_prompt(scope=self.memory_scope)
        enhanced_input = user_input
        if memory_text:
            enhanced_input = f"{memory_text}\n\n---\n\n用户输入: {user_input}"

        messages.append(ChatMessage(role="user", content=enhanced_input))
        return messages

    def _get_tool_definitions(self, tools: list[str] | None = None) -> list[ToolDefinition] | None:
        """获取工具定义列表"""
        if tools:
            # 只返回指定的工具
            definitions = []
            for name in tools:
                definition = self.tool_registry.get_definition(name)
                if definition:
                    definitions.append(definition)
            return definitions if definitions else None
        else:
            # 返回所有已注册的工具
            all_defs = self.tool_registry.get_all_definitions()
            return all_defs if all_defs else None

    async def _think(
        self,
        messages: list[ChatMessage],
        tools: list[ToolDefinition] | None,
    ) -> ChatResponse:
        """调用 LLM 进行推理"""
        try:
            return await self.client.chat(messages, tools, save_history=False)
        finally:
            # Budget preparation can compress both history and current messages.
            self._capture_checkpoint(messages)

    async def _think_stream(
        self,
        messages: list[ChatMessage],
        tools: list[ToolDefinition] | None,
        callback,
    ) -> ChatResponse:
        """调用 LLM 进行推理（流式输出到回调）"""
        accumulator = StreamAccumulator()

        try:
            stream = self.client.chat_stream(messages, tools, save_history=False)
            async with aclosing(stream):
                async for chunk in stream:
                    accumulator.add(chunk)
                    if chunk.delta_content:
                        if callback:
                            await callback(chunk.delta_content)
        finally:
            self._capture_checkpoint(messages)
        return accumulator.response()

    async def _act(
        self,
        tool_calls: list[ToolCall],
        messages: list[ChatMessage] | None = None,
    ) -> list[ToolResult]:
        """执行工具调用

        LLM 提出依赖，代码校验并限制副作用与并发数。

        Args:
            tool_calls: 要执行的工具调用列表

        Returns:
            list[ToolResult]: 执行结果列表
        """

        async def execute_one(tc: ToolCall) -> ToolResult:
            result = await self.tool_registry.execute(tc.function.name, tc.function.arguments)
            if self.event_bus:
                try:
                    await self.event_bus.emit(
                        Event(
                            type="tool.executed",
                            source=self.agent_name,
                            data={
                                "tool": tc.function.name,
                                "run_id": self.last_trace.run_id if self.last_trace else None,
                            },
                        )
                    )
                except Exception as exc:
                    logger.warning("工具事件订阅失败：{}", type(exc).__name__)
            return ToolResult(tool_call_id=tc.id, content=result, name=tc.function.name)

        return await self.tool_scheduler.execute(
            tool_calls, messages or [], execute_one, journal=self._journal
        )

    # ─── 便捷方法 ──────────────────────────────────────

    def register_tool(self, tool: Tool) -> None:
        """注册一个工具到 Agent"""
        self.tool_registry.add_tool(tool)

    async def _extract_memories(self, user_input: str) -> None:
        """Reconcile explicit user facts before execution; assistant guesses are not evidence."""
        items = self.conversation_memory.get_all(scope=self.memory_scope)
        existing = [
            {
                "key": item["key"],
                "value": item["value"],
                "scope": "project" if item.get("scope", "") else "user",
            }
            for item in items
            if item["type"] == "kv"
        ]
        budget = min(6000, self.client.compressor.input_budget) if self.client.compressor else 6000
        try:
            memories = await self.memory_extractor.extract(
                user_input,
                existing=existing,
                input_budget=budget,
            )
        except Exception as e:
            logger.warning(f"[{self.agent_name}] 记忆提取失败: {e}")
            return

        for m in memories:
            scope = (self.memory_scope or "project:default") if m["scope"] == "project" else ""
            if m["action"] == "forget":
                self.conversation_memory.forget(m["key"], scope=scope)
            else:
                self.conversation_memory.remember(m["key"], m["value"], scope=scope)

        if memories:
            logger.info(
                f"[{self.agent_name}] 维护 {len(memories)} 条长期记忆: "
                f"{[m['key'] for m in memories]}"
            )

    def remember(self, key: str, value: Any, *, scope: str = "") -> None:
        """让 Agent 记住一个信息"""
        self.conversation_memory.remember(key, value, scope=scope)

    def recall(self, key: str, *, scope: str = "") -> Any | None:
        """让 Agent 回忆一个信息"""
        return self.conversation_memory.recall(key, scope=scope)

    def forget(self, key: str, *, scope: str = "") -> bool:
        return self.conversation_memory.forget(key, scope=scope)

    def stop(self) -> None:
        """停止 Agent 执行"""
        self._is_running = False
        logger.info(f"[{self.agent_name}] 已收到停止信号")

    def reset(self) -> None:
        """重置 Agent 状态"""
        self.client.clear_history()
        self.conversation_memory.clear()
        self.working_memory.clear()
        self._is_running = False
        logger.info(f"[{self.agent_name}] 已重置")
