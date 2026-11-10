"""
规划器 - 任务分解和执行计划

职责：
- 将复杂任务分解为可执行的步骤
- 管理步骤之间的依赖关系
- 追踪执行进度

对应 pi-mono 中 Agent 的任务规划能力。
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from loguru import logger
from pydantic import BaseModel, Field


class StepStatus(str, Enum):
    """步骤状态"""
    PENDING = "pending"       # 等待执行
    RUNNING = "running"       # 正在执行
    COMPLETED = "completed"   # 已完成
    FAILED = "failed"         # 执行失败
    SKIPPED = "skipped"       # 已跳过


class TaskStep(BaseModel):
    """一个执行步骤"""
    id: str = Field(..., description="步骤 ID")
    description: str = Field(..., description="步骤描述")
    tool_name: str | None = Field(default=None, description="需要调用的工具名称")
    tool_args: dict[str, Any] = Field(
        default_factory=dict, description="工具参数"
    )
    depends_on: list[str] = Field(
        default_factory=list, description="依赖的步骤 ID 列表"
    )
    status: StepStatus = Field(default=StepStatus.PENDING, description="当前状态")
    result: str | None = Field(default=None, description="执行结果")
    error: str | None = Field(default=None, description="错误信息")


class TaskPlan(BaseModel):
    """任务计划 - 包含多个步骤"""
    task_id: str = Field(..., description="任务 ID")
    description: str = Field(..., description="任务描述")
    steps: list[TaskStep] = Field(
        default_factory=list, description="执行步骤列表"
    )
    created_at: str = Field(default="", description="创建时间")


class Planner:
    """规划器 - 管理任务分解和执行

    设计理念：
    - 支持手动规划（用户指定步骤）
    - 支持 AI 辅助规划（LLM 自动分解任务）
    - 简单的 DAG 依赖管理
    """

    def __init__(self):
        self._plans: dict[str, TaskPlan] = {}
        self._current_plan_id: str | None = None

    def create_plan(self, task_id: str, description: str) -> TaskPlan:
        """创建一个新的任务计划

        Args:
            task_id: 任务唯一 ID
            description: 任务描述

        Returns:
            TaskPlan: 创建的计划
        """
        from datetime import datetime

        plan = TaskPlan(
            task_id=task_id,
            description=description,
            created_at=datetime.now().isoformat(),
        )
        self._plans[task_id] = plan
        self._current_plan_id = task_id
        logger.info(f"创建计划: {task_id} - {description}")
        return plan

    def add_step(
        self,
        task_id: str,
        step_id: str,
        description: str,
        *,
        tool_name: str | None = None,
        tool_args: dict[str, Any] | None = None,
        depends_on: list[str] | None = None,
    ) -> TaskStep:
        """向计划中添加一个步骤

        Args:
            task_id: 任务 ID
            step_id: 步骤 ID（在计划内唯一）
            description: 步骤描述
            tool_name: 需要的工具
            tool_args: 工具参数
            depends_on: 依赖的步骤 ID

        Returns:
            TaskStep: 创建的步骤
        """
        plan = self._plans[task_id]
        step = TaskStep(
            id=step_id,
            description=description,
            tool_name=tool_name,
            tool_args=tool_args or {},
            depends_on=depends_on or [],
        )
        plan.steps.append(step)
        logger.debug(f"添加步骤: {task_id}/{step_id} - {description}")
        return step

    def get_next_step(self, task_id: str) -> TaskStep | None:
        """获取下一个可执行的步骤

        可执行条件：
        1. 状态为 PENDING
        2. 所有依赖步骤都已完成

        Args:
            task_id: 任务 ID

        Returns:
            TaskStep | None: 下一个可执行的步骤
        """
        plan = self._plans.get(task_id)
        if plan is None:
            return None

        for step in plan.steps:
            if step.status != StepStatus.PENDING:
                continue

            # 检查依赖
            deps_met = all(
                self._get_step_status(task_id, dep_id) == StepStatus.COMPLETED
                for dep_id in step.depends_on
            )
            if deps_met:
                return step

        return None

    def _get_step_status(self, task_id: str, step_id: str) -> StepStatus | None:
        """获取某步骤的状态"""
        plan = self._plans.get(task_id)
        if plan is None:
            return None
        for step in plan.steps:
            if step.id == step_id:
                return step.status
        return None

    def update_step(
        self,
        task_id: str,
        step_id: str,
        *,
        status: StepStatus | None = None,
        result: str | None = None,
        error: str | None = None,
    ) -> None:
        """更新步骤状态"""
        plan = self._plans.get(task_id)
        if plan is None:
            return
        for step in plan.steps:
            if step.id == step_id:
                if status is not None:
                    step.status = status
                if result is not None:
                    step.result = result
                if error is not None:
                    step.error = error
                logger.debug(f"步骤更新: {task_id}/{step_id} → {step.status}")
                return

    def get_plan_summary(self, task_id: str) -> str:
        """获取计划的摘要信息"""
        plan = self._plans.get(task_id)
        if plan is None:
            return f"计划 {task_id} 不存在"

        lines = [
            f"## 任务计划: {task_id}",
            f"描述: {plan.description}",
            f"创建时间: {plan.created_at}",
            f"步骤进度:",
        ]
        status_icons = {
            StepStatus.PENDING: "⏳",
            StepStatus.RUNNING: "🔄",
            StepStatus.COMPLETED: "✅",
            StepStatus.FAILED: "❌",
            StepStatus.SKIPPED: "⏭️",
        }
        for step in plan.steps:
            icon = status_icons.get(step.status, "❓")
            deps = f" (依赖: {', '.join(step.depends_on)})" if step.depends_on else ""
            lines.append(f"  {icon} {step.id}: {step.description}{deps}")
            if step.error:
                lines.append(f"     错误: {step.error}")

        return "\n".join(lines)

    def is_completed(self, task_id: str) -> bool:
        """检查计划是否全部完成"""
        plan = self._plans.get(task_id)
        if plan is None:
            return False
        return all(
            step.status in (StepStatus.COMPLETED, StepStatus.SKIPPED)
            for step in plan.steps
        )
