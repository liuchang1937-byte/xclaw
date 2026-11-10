"""
Agent 核心模块 - 对应 pi-mono 中的 pi-agent-core

职责：
- Agent 基类：定义 Agent 的生命周期
- 工具系统：Tool 的定义、注册和执行
- 记忆管理：短期/长期记忆
- 规划器：任务分解和执行

设计理念：
- 一切皆工具：Agent 的能力通过工具来扩展
- 透明执行：每个步骤都可观测、可追溯
- 最小核心：框架只提供必要的抽象，不强迫使用模式
"""

from xclaw.core.agent import Agent
from xclaw.core.memory import ConversationMemory, Memory
from xclaw.core.memory_extractor import MemoryExtractor
from xclaw.core.planner import Planner, TaskPlan, TaskStep
from xclaw.core.skill import Skill, SkillLoader, register_skills_as_tools
from xclaw.core.tool import RiskLevel, Tool, register_tool, tool_registry

__all__ = [
    "Agent",
    "Tool",
    "RiskLevel",
    "tool_registry",
    "register_tool",
    "Memory",
    "ConversationMemory",
    "MemoryExtractor",
    "Planner",
    "TaskPlan",
    "TaskStep",
    "Skill",
    "SkillLoader",
    "register_skills_as_tools",
]
