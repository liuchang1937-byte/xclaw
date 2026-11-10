"""
AI Provider - 多 LLM 提供商适配

设计理念：
- 统一的接口，多种后端实现
- 支持 OpenAI 兼容协议（OpenAI / DeepSeek / 通义千问 / Moonshot 等）
- Provider 注册机制，方便扩展
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, AsyncIterator

from openai import AsyncOpenAI

from xclaw.ai.types import ChatMessage, ChatResponse, StreamChunk, ToolDefinition
from xclaw.telemetry import current_model_purpose


class BaseProvider(ABC):
    """LLM Provider 抽象基类"""

    @abstractmethod
    async def chat(
        self,
        messages: list[ChatMessage],
        tools: list[ToolDefinition] | None = None,
        *,
        model: str,
        temperature: float = 0.7,
        max_tokens: int = 4096,
    ) -> ChatResponse:
        """发送聊天请求，返回完整响应"""
        ...

    @abstractmethod
    async def chat_stream(
        self,
        messages: list[ChatMessage],
        tools: list[ToolDefinition] | None = None,
        *,
        model: str,
        temperature: float = 0.7,
        max_tokens: int = 4096,
    ) -> AsyncIterator[StreamChunk]:
        """发送聊天请求，返回流式响应"""
        ...


class OpenAIProvider(BaseProvider):
    """OpenAI 兼容协议 Provider

    支持所有兼容 OpenAI API 协议的服务：
    - OpenAI (GPT-4, GPT-4o, etc.)
    - DeepSeek (deepseek-chat, deepseek-reasoner)
    - 通义千问 (qwen-*)
    - Moonshot (moonshot-v1-*)
    - 本地部署的 vLLM / Ollama 等
    """

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.openai.com/v1",
        *,
        auxiliary_thinking: bool | None = False,
        judge_json_mode: bool = True,
    ):
        """
        初始化 OpenAI 兼容 Provider

        Args:
            api_key: API 密钥
            base_url: API 基础 URL（用于切换不同的服务端点）
        """
        self.api_key = api_key
        self.base_url = base_url
        self.auxiliary_thinking = auxiliary_thinking
        self.judge_json_mode = judge_json_mode
        # 创建异步客户端
        self._client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
        )

    def _build_request_kwargs(
        self,
        messages: list[ChatMessage],
        tools: list[ToolDefinition] | None,
        model: str,
        temperature: float,
        max_tokens: int,
    ) -> dict[str, Any]:
        """构建 API 请求参数"""
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": [m.to_openai_format() for m in messages],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if tools:
            kwargs["tools"] = [t.to_openai_format() for t in tools]
        if (
            model.startswith("deepseek")
            and self.auxiliary_thinking is not None
            and current_model_purpose() in {"compression", "scheduling", "memory", "judge"}
        ):
            kwargs["extra_body"] = {
                "thinking": {"type": "enabled" if self.auxiliary_thinking else "disabled"}
            }
        if current_model_purpose() == "judge" and getattr(self, "judge_json_mode", True):
            kwargs["response_format"] = {"type": "json_object"}
        return kwargs

    def _parse_response(self, response: Any) -> ChatResponse:
        """解析 OpenAI 响应为 ChatResponse"""
        choice = response.choices[0]
        message = choice.message

        return ChatResponse(
            content=message.content,
            reasoning_content=getattr(message, "reasoning_content", None),
            tool_calls=[
                {
                    "id": tc.id,
                    "type": tc.type,
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                }
                for tc in (message.tool_calls or [])
            ],
            finish_reason=choice.finish_reason,
            usage={
                "prompt_tokens": response.usage.prompt_tokens,
                "completion_tokens": response.usage.completion_tokens,
                "total_tokens": response.usage.total_tokens,
            }
            if response.usage
            else None,
        )

    async def chat(
        self,
        messages: list[ChatMessage],
        tools: list[ToolDefinition] | None = None,
        *,
        model: str,
        temperature: float = 0.7,
        max_tokens: int = 4096,
    ) -> ChatResponse:
        """发送聊天请求，返回完整响应"""
        kwargs = self._build_request_kwargs(messages, tools, model, temperature, max_tokens)
        response = await self._client.chat.completions.create(**kwargs)
        return self._parse_response(response)

    async def chat_stream(
        self,
        messages: list[ChatMessage],
        tools: list[ToolDefinition] | None = None,
        *,
        model: str,
        temperature: float = 0.7,
        max_tokens: int = 4096,
    ) -> AsyncIterator[StreamChunk]:
        """发送聊天请求，流式返回"""
        kwargs = self._build_request_kwargs(messages, tools, model, temperature, max_tokens)
        kwargs["stream"] = True
        kwargs["stream_options"] = {"include_usage": True}

        stream = await self._client.chat.completions.create(**kwargs)

        # 流式处理状态
        tool_calls_buffer: dict[int, dict] = {}

        async for chunk in stream:
            if getattr(chunk, "usage", None):
                yield StreamChunk(
                    usage={
                        "prompt_tokens": chunk.usage.prompt_tokens,
                        "completion_tokens": chunk.usage.completion_tokens,
                        "total_tokens": chunk.usage.total_tokens,
                    }
                )
            delta = chunk.choices[0].delta if chunk.choices else None
            if delta is None:
                continue

            # 处理文本增量
            delta_content = delta.content or None

            # 处理工具调用增量
            delta_tool_calls = None
            if delta.tool_calls:
                tc_list = []
                for tc_delta in delta.tool_calls:
                    idx = tc_delta.index
                    if idx not in tool_calls_buffer:
                        tool_calls_buffer[idx] = {
                            "id": tc_delta.id or "",
                            "type": "function",
                            "function": {"name": "", "arguments": ""},
                        }
                    buf = tool_calls_buffer[idx]
                    if tc_delta.id:
                        buf["id"] = tc_delta.id
                    if tc_delta.function:
                        if tc_delta.function.name:
                            buf["function"]["name"] += tc_delta.function.name
                        if tc_delta.function.arguments:
                            buf["function"]["arguments"] += tc_delta.function.arguments
                    tc_list.append(
                        {
                            "index": idx,
                            "id": buf["id"],
                            "type": "function",
                            "function": {
                                "name": buf["function"]["name"],
                                "arguments": buf["function"]["arguments"],
                            },
                        }
                    )
                delta_tool_calls = tc_list

            yield StreamChunk(
                delta_content=delta_content,
                delta_reasoning_content=getattr(delta, "reasoning_content", None),
                delta_tool_calls=delta_tool_calls,
                finish_reason=chunk.choices[0].finish_reason,
            )


class ProviderRegistry:
    """Provider 注册中心

    管理多个 LLM Provider 实例，支持按名称切换。
    使用方式：
        registry = ProviderRegistry()
        registry.register("deepseek", OpenAIProvider(key, "https://api.deepseek.com/v1"))
        registry.register("qwen", OpenAIProvider(key, "https://dashscope.aliyuncs.com/compatible-mode/v1"))
        provider = registry.get("deepseek")
    """

    def __init__(self):
        self._providers: dict[str, BaseProvider] = {}

    def register(self, name: str, provider: BaseProvider) -> None:
        """注册一个 Provider"""
        self._providers[name] = provider

    def get(self, name: str) -> BaseProvider:
        """获取指定名称的 Provider"""
        if name not in self._providers:
            available = ", ".join(self._providers.keys())
            raise KeyError(f"Provider '{name}' 未注册。可用的 Provider: {available}")
        return self._providers[name]

    def list_providers(self) -> list[str]:
        """列出所有已注册的 Provider 名称"""
        return list(self._providers.keys())
