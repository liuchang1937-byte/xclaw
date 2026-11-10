"""
工具系统的测试
"""

import json

import pytest

from xclaw.core.tool import Tool, ToolRegistry, register_tool


# ─── 测试用函数 ──────────────────────────────────────

async def mock_search(query: str, max_results: int = 5) -> str:
    """模拟搜索函数"""
    return f"搜索 '{query}' 的结果（共 {max_results} 条）"


def sync_calculator(a: int, b: int, operation: str = "add") -> str:
    """同步计算器函数"""
    if operation == "add":
        return str(a + b)
    elif operation == "multiply":
        return str(a * b)
    return "Unknown operation"


async def failing_tool() -> str:
    """会失败的工具"""
    raise ValueError("工具执行失败")


# ─── 测试 Tool 类 ────────────────────────────────────

class TestTool:
    """工具类的单元测试"""

    def test_tool_creation(self):
        """测试工具创建"""
        tool = Tool(
            name="test_search",
            description="搜索测试",
            func=mock_search,
        )
        assert tool.name == "test_search"
        assert tool.description == "搜索测试"
        assert "query" in tool.parameters.get("properties", {})

    def test_tool_schema_auto_generation(self):
        """测试自动生成的 JSON Schema"""
        tool = Tool(
            name="calc",
            description="计算器",
            func=sync_calculator,
        )
        schema = tool.parameters
        assert schema["type"] == "object"
        assert "a" in schema["properties"]
        assert "b" in schema["properties"]
        assert "operation" in schema["properties"]
        # a 和 b 是必填参数
        assert "a" in schema["required"]
        assert "b" in schema["required"]
        # operation 有默认值，不是必填
        assert "operation" not in schema["required"]

    def test_tool_to_definition(self):
        """测试转换为 ToolDefinition"""
        tool = Tool(
            name="test",
            description="测试",
            func=mock_search,
        )
        definition = tool.to_definition()
        assert definition.name == "test"
        assert definition.description == "测试"

    @pytest.mark.asyncio
    async def test_tool_execute(self):
        """测试工具执行"""
        tool = Tool(
            name="test",
            description="测试",
            func=mock_search,
        )
        result = await tool.execute(query="AI", max_results=10)
        assert "AI" in result
        assert "10" in result

    @pytest.mark.asyncio
    async def test_tool_execute_error(self):
        """测试工具执行失败的情况"""
        tool = Tool(
            name="failing",
            description="会失败的工具",
            func=failing_tool,
        )
        result = await tool.execute()
        assert "工具执行失败" in result or "失败" in result


# ─── 测试 ToolRegistry ──────────────────────────────

class TestToolRegistry:
    """工具注册中心的测试"""

    def test_register_tool(self):
        """测试注册工具"""
        registry = ToolRegistry()
        tool = Tool(
            name="test",
            description="测试",
            func=mock_search,
        )
        registry.add_tool(tool)
        assert "test" in registry
        assert len(registry) == 1

    def test_get_tool(self):
        """测试获取工具"""
        registry = ToolRegistry()
        tool = Tool(
            name="test",
            description="测试",
            func=mock_search,
        )
        registry.add_tool(tool)

        retrieved = registry.get("test")
        assert retrieved is not None
        assert retrieved.name == "test"

        # 获取不存在的工具
        assert registry.get("nonexistent") is None

    def test_list_tools(self):
        """测试列出所有工具"""
        registry = ToolRegistry()
        registry.add_tool(Tool(name="tool1", description="工具1", func=mock_search))
        registry.add_tool(Tool(name="tool2", description="工具2", func=mock_search))

        tools = registry.list_tools()
        assert len(tools) == 2
        assert "tool1" in tools
        assert "tool2" in tools

    def test_get_all_definitions(self):
        """测试获取所有工具定义"""
        registry = ToolRegistry()
        registry.add_tool(Tool(name="t1", description="d1", func=mock_search))
        registry.add_tool(Tool(name="t2", description="d2", func=mock_search))

        definitions = registry.get_all_definitions()
        assert len(definitions) == 2
        names = [d.name for d in definitions]
        assert "t1" in names

    @pytest.mark.asyncio
    async def test_execute_by_name(self):
        """测试通过名称执行工具（解析 JSON 参数）"""
        registry = ToolRegistry()
        registry.add_tool(Tool(name="calc", description="计算器", func=sync_calculator))

        result = await registry.execute(
            "calc",
            json.dumps({"a": 1, "b": 2, "operation": "add"}),
        )
        assert "3" in result

    @pytest.mark.asyncio
    async def test_execute_nonexistent(self):
        """测试执行不存在的工具"""
        registry = ToolRegistry()
        result = await registry.execute("nonexistent", "{}")
        assert "错误" in result or "未找到" in result


# ─── 测试装饰器注册 ──────────────────────────────────

def test_decorator_registration():
    """测试装饰器方式注册工具（使用新注册中心避免全局污染）"""
    registry = ToolRegistry()

    @registry.register(description="测试装饰器")
    def my_tool(x: str) -> str:
        return f"结果: {x}"

    assert "my_tool" in registry
    tool = registry.get("my_tool")
    assert tool is not None
    assert tool.description == "测试装饰器"
