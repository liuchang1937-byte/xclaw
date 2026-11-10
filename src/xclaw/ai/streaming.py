"""聚合 Provider 给出的累计工具调用快照，兼容旧的无索引完整调用。"""

from xclaw.ai.types import ChatResponse, StreamChunk, ToolCall


class StreamAccumulator:
    def __init__(self):
        self.parts: list[str] = []
        self.reasoning_parts: list[str] = []
        self.calls: dict[int, ToolCall] = {}
        self.finish_reason: str | None = None
        self.usage = None

    def add(self, chunk: StreamChunk) -> None:
        if chunk.usage is not None:
            self.usage = chunk.usage
        if chunk.delta_content:
            self.parts.append(chunk.delta_content)
        if chunk.delta_reasoning_content is not None:
            self.reasoning_parts.append(chunk.delta_reasoning_content)
        if chunk.finish_reason:
            self.finish_reason = chunk.finish_reason
        for call in chunk.delta_tool_calls or []:
            index = call.index
            if index is None:
                if not call.id:
                    raise ValueError("流式工具调用缺少索引和 ID")
                index = next(
                    (key for key, value in self.calls.items() if value.id == call.id), None
                )
                if index is None:
                    index = max(self.calls, default=-1) + 1
            self.calls[index] = call.model_copy(deep=True)

    def response(self) -> ChatResponse:
        calls = [
            self.calls[index].model_copy(update={"index": None}) for index in sorted(self.calls)
        ]
        if any(not call.id or not call.function.name for call in calls):
            raise ValueError("流式工具调用不完整，未执行工具")
        if len({call.id for call in calls}) != len(calls):
            raise ValueError("流式工具调用 ID 重复，未执行工具")
        return ChatResponse(
            content="".join(self.parts) or None,
            tool_calls=calls,
            finish_reason=self.finish_reason,
            usage=self.usage,
            reasoning_content="".join(self.reasoning_parts) if self.reasoning_parts else None,
        )
