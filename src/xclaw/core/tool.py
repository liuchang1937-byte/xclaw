"""
工具系统 - Agent 的能力扩展机制

设计理念：
- 一切能力都通过工具暴露给 Agent
- 工具是纯函数：输入 → 输出，无副作用（理想情况下）
- 注册机制：装饰器注册，简单直观
- JSON Schema 自动生成：工具参数由类型注解自动推导

对应 pi-mono 中 Agent 的 tool 管理机制。
"""

from __future__ import annotations

import inspect
import json
from enum import Enum
from typing import Any, Callable, get_type_hints

from loguru import logger

from xclaw.ai.types import ToolDefinition
from xclaw.core.concurrency import run_blocking
from xclaw.core.execution import Capability, ProcessPlan, RunProcess


class RiskLevel(str, Enum):
    """兼容旧工具的副作用声明；新路由使用 Capability 与进程请求。"""

    READ_ONLY = "read_only"
    WORKSPACE_WRITE = "workspace_write"
    CODE_EXECUTION = "code_execution"


class Tool:
    """工具 - Agent 可以调用的一个能力单元

    每个 Tool 包含：
    - name: 工具的唯一标识名
    - description: 工具的功能描述（LLM 据此判断何时调用）
    - parameters: 参数的 JSON Schema
    - func: 实际的执行函数
    """

    def __init__(
        self,
        name: str,
        description: str,
        func: Callable,
        parameters: dict[str, Any] | None = None,
        risk_level: RiskLevel | None = None,
        path_parameters: tuple[str, ...] = (),
        capabilities: frozenset[Capability] | None = None,
        request_builder: Callable[..., ProcessPlan | RunProcess] | None = None,
        parallel_safe: bool = False,
    ):
        """
        初始化工具

        Args:
            name: 工具名称（全局唯一）
            description: 工具描述（越详细越好，帮助 LLM 正确使用）
            func: 工具的执行函数（支持同步和异步）
            parameters: 参数的 JSON Schema，为 None 则自动生成
        """
        self.name = name
        self.description = description
        self.func = func
        self.parameters = parameters or self._build_schema(func)
        self.risk_level = risk_level or RiskLevel.READ_ONLY
        self.path_parameters = path_parameters
        legacy = {
            RiskLevel.READ_ONLY: frozenset({Capability.FILE_READ}),
            RiskLevel.WORKSPACE_WRITE: frozenset({Capability.FILE_READ, Capability.FILE_WRITE}),
            RiskLevel.CODE_EXECUTION: frozenset(
                {
                    Capability.FILE_READ,
                    Capability.FILE_WRITE,
                    Capability.PROCESS,
                }
            ),
        }
        self.capabilities = (
            frozenset(Capability(cap) for cap in capabilities)
            if capabilities is not None
            else legacy.get(risk_level)
        )
        self.request_builder = request_builder
        self.parallel_safe = parallel_safe

    @property
    def supports_parallel(self) -> bool:
        """只有可信实现明确声明的纯提示词/只读工具允许并行。"""
        return bool(
            self.parallel_safe
            and self.capabilities
            and self.capabilities <= {Capability.FILE_READ, Capability.PROMPT}
            and self.request_builder is None
        )

    @staticmethod
    def _build_schema(func: Callable) -> dict[str, Any]:
        """从函数签名自动生成 JSON Schema

        支持的类型映射：
        - str → {"type": "string"}
        - int → {"type": "integer"}
        - float → {"type": "number"}
        - bool → {"type": "boolean"}
        - list → {"type": "array"}
        - dict → {"type": "object"}
        """
        sig = inspect.signature(func)
        hints = get_type_hints(func)
        properties = {}
        required = []

        for param_name, param in sig.parameters.items():
            # 跳过 self / cls
            if param_name in ("self", "cls"):
                continue
            if param.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
                continue

            annotation = hints.get(param_name, param.annotation)
            if annotation is inspect.Parameter.empty:
                annotation = str  # 默认当作字符串

            # 类型映射
            type_map = {
                str: "string",
                int: "integer",
                float: "number",
                bool: "boolean",
                list: "array",
                dict: "object",
            }
            json_type = type_map.get(annotation, "string")

            properties[param_name] = {"type": json_type}

            # 没有默认值的参数是必填的
            if param.default is inspect.Parameter.empty:
                required.append(param_name)

        return {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        }

    def bind_arguments(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """先校验模型参数，拒绝错误类型和额外参数。"""
        if not isinstance(arguments, dict):
            raise ValueError("工具参数必须是 JSON 对象")
        properties = self.parameters.get("properties", {})
        unknown = arguments.keys() - properties.keys()
        if unknown:
            raise ValueError(f"未知工具参数: {', '.join(sorted(unknown))}")
        for name, value in arguments.items():
            expected = properties[name].get("type")
            types = {
                "string": str,
                "integer": int,
                "number": (int, float),
                "boolean": bool,
                "array": list,
                "object": dict,
            }
            if expected in types and (
                not isinstance(value, types[expected])
                or expected in ("integer", "number")
                and isinstance(value, bool)
            ):
                raise ValueError(f"参数 {name} 必须是 {expected}")
        signature = inspect.signature(self.func)
        bound = signature.bind(**arguments)
        bound.apply_defaults()
        return {
            name: value
            for name, value in bound.arguments.items()
            if signature.parameters[name].kind
            not in (
                inspect.Parameter.VAR_POSITIONAL,
                inspect.Parameter.VAR_KEYWORD,
            )
        }

    def to_definition(self) -> ToolDefinition:
        """转换为工具定义"""
        return ToolDefinition(
            name=self.name,
            description=self.description,
            parameters=self.parameters,
        )

    async def execute(self, **kwargs) -> str:
        """执行工具，返回字符串结果

        Args:
            **kwargs: 工具参数

        Returns:
            str: 执行结果（序列化为字符串）
        """
        try:
            if self.request_builder is not None:
                return "错误: 进程工具必须通过执行后端调用"
            kwargs = self.bind_arguments(kwargs)
            logger.debug(f"执行工具: {self.name}({json.dumps(kwargs, ensure_ascii=False)})")

            # 支持同步和异步函数
            if self.supports_parallel and not inspect.iscoroutinefunction(self.func):
                result = await run_blocking(self.func, **kwargs)
            else:
                result = self.func(**kwargs)
            if inspect.iscoroutine(result):
                result = await result

            # 统一转为字符串返回
            if isinstance(result, str):
                return result
            return json.dumps(result, ensure_ascii=False, indent=2)

        except Exception as e:
            error_msg = f"工具 {self.name} 执行失败: {e}"
            logger.error(error_msg)
            return error_msg

    def __repr__(self) -> str:
        return f"Tool(name='{self.name}', desc='{self.description[:50]}...')"


class ToolRegistry:
    """工具注册中心

    全局单例，管理所有已注册的工具。
    支持装饰器注册和直接注册两种方式。
    """

    def __init__(self):
        self._tools: dict[str, Tool] = {}
        self._sandbox_router = None

    def set_sandbox_router(self, router) -> None:
        """绑定 Agent 私有的风险路由器，避免使用全局活动沙箱。"""
        self._sandbox_router = router

    def register(
        self,
        name: str | None = None,
        description: str = "",
        parameters: dict[str, Any] | None = None,
        risk_level: RiskLevel | None = None,
        path_parameters: tuple[str, ...] = (),
        capabilities: frozenset[Capability] | None = None,
        request_builder: Callable[..., ProcessPlan | RunProcess] | None = None,
        parallel_safe: bool = False,
    ) -> Callable:
        """装饰器：注册一个函数为工具

        使用方式：
            @tool_registry.register(description="搜索网页内容")
            def web_search(query: str) -> str:
                ...

        Args:
            name: 工具名称（默认使用函数名）
            description: 工具描述
            parameters: 参数 Schema（默认自动生成）
        """

        def decorator(func: Callable) -> Callable:
            tool_name = name or func.__name__
            tool = Tool(
                name=tool_name,
                description=description or func.__doc__ or "",
                func=func,
                parameters=parameters,
                risk_level=risk_level,
                path_parameters=path_parameters,
                capabilities=capabilities,
                request_builder=request_builder,
                parallel_safe=parallel_safe,
            )
            self._tools[tool_name] = tool
            logger.info(f"注册工具: {tool_name}")
            return func

        return decorator

    def add_tool(self, tool: Tool) -> None:
        """直接添加一个工具实例"""
        self._tools[tool.name] = tool
        logger.info(f"添加工具: {tool.name}")

    def get(self, name: str) -> Tool | None:
        """获取指定名称的工具"""
        return self._tools.get(name)

    def get_definition(self, name: str) -> ToolDefinition | None:
        """获取工具的定义（给 LLM 使用）"""
        tool = self._tools.get(name)
        if (
            tool
            and self._sandbox_router
            and not self._sandbox_router.policy.allows(tool.capabilities)
        ):
            return None
        return tool.to_definition() if tool else None

    def get_all_definitions(self) -> list[ToolDefinition]:
        """获取所有工具的定义"""
        return [
            t.to_definition()
            for t in self._tools.values()
            if not self._sandbox_router or self._sandbox_router.policy.allows(t.capabilities)
        ]

    def list_tools(self) -> list[str]:
        """列出所有注册的工具名称"""
        return [definition.name for definition in self.get_all_definitions()]

    async def execute(self, name: str, arguments: str) -> str:
        """根据名称和 JSON 参数字符串执行工具

        Args:
            name: 工具名称
            arguments: JSON 格式的参数字符串

        Returns:
            str: 执行结果
        """
        tool = self._tools.get(name)
        if tool is None:
            return f"错误: 未找到工具 '{name}'。可用的工具: {self.list_tools()}"

        try:
            kwargs = json.loads(arguments) if arguments else {}
        except json.JSONDecodeError as e:
            return f"错误: 参数 JSON 解析失败: {e}"

        if not isinstance(kwargs, dict):
            return "错误: 工具参数必须是 JSON 对象"

        if self._sandbox_router is not None:
            return await self._sandbox_router.execute(tool, kwargs)
        return await tool.execute(**kwargs)

    def __contains__(self, name: str) -> bool:
        return name in self._tools

    def __len__(self) -> int:
        return len(self._tools)


# 全局工具注册中心（单例）
tool_registry = ToolRegistry()


def register_tool(
    name: str | None = None,
    description: str = "",
    risk_level: RiskLevel | None = None,
    path_parameters: tuple[str, ...] = (),
    capabilities: frozenset[Capability] | None = None,
    request_builder: Callable[..., ProcessPlan | RunProcess] | None = None,
    parallel_safe: bool = False,
) -> Callable:
    """便捷的装饰器函数，向全局注册中心注册工具

    使用方式：
        @register_tool(description="读取文件内容")
        def read_file(path: str) -> str:
            ...

    这是最常用的注册方式。
    """
    return tool_registry.register(
        name=name,
        description=description,
        risk_level=risk_level,
        path_parameters=path_parameters,
        capabilities=capabilities,
        request_builder=request_builder,
        parallel_safe=parallel_safe,
    )
