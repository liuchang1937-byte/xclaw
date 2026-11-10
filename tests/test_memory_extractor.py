"""
记忆提取器的测试
"""

import pytest

from xclaw.ai.provider import BaseProvider
from xclaw.ai.types import ChatResponse, StreamChunk
from xclaw.core.memory_extractor import MemoryExtractor


class FakeProvider(BaseProvider):
    """返回预设内容的假 Provider"""

    def __init__(self, content: str = ""):
        self.content = content

    async def chat(
        self, messages, tools=None, *, model, temperature=0.7, max_tokens=4096
    ):
        return ChatResponse(content=self.content)

    async def chat_stream(
        self, messages, tools=None, *, model, temperature=0.7, max_tokens=4096
    ):
        yield StreamChunk(delta_content=self.content)


class TestMemoryExtractor:
    """记忆提取的解析逻辑（用假 Provider，不真实调用 LLM）"""

    @pytest.mark.asyncio
    async def test_extract_normal_json(self):
        provider = FakeProvider('[{"key":"user_name","value":"张三"}]')
        extractor = MemoryExtractor(provider)
        result = await extractor.extract("用户: 我叫张三")
        assert result == [{"key": "user_name", "value": "张三"}]

    @pytest.mark.asyncio
    async def test_extract_empty(self):
        provider = FakeProvider("[]")
        extractor = MemoryExtractor(provider)
        result = await extractor.extract("用户: 你好")
        assert result == []

    @pytest.mark.asyncio
    async def test_extract_with_code_fence(self):
        # LLM 输出带 ```json 包裹
        provider = FakeProvider('```json\n[{"key":"stack","value":"python"}]\n```')
        extractor = MemoryExtractor(provider)
        result = await extractor.extract("...")
        assert result == [{"key": "stack", "value": "python"}]

    @pytest.mark.asyncio
    async def test_extract_garbage(self):
        # LLM 输出纯文本，应降级返回 []
        provider = FakeProvider("我觉得没什么需要记的")
        extractor = MemoryExtractor(provider)
        result = await extractor.extract("...")
        assert result == []

    @pytest.mark.asyncio
    async def test_extract_llm_error(self):
        # LLM 调用抛异常，应返回 [] 而不是崩溃
        class BrokenProvider(BaseProvider):
            async def chat(self, *args, **kwargs):
                raise RuntimeError("boom")

            async def chat_stream(self, *args, **kwargs):
                if False:
                    yield

        extractor = MemoryExtractor(BrokenProvider())
        result = await extractor.extract("...")
        assert result == []
