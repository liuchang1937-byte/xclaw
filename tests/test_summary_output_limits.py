from xclaw.ai.context import ContextCompressor
from xclaw.ai.types import ChatMessage, ChatResponse


class LengthProvider:
    def __init__(self):
        self.calls = 0

    async def chat(self, *args, **kwargs):
        self.calls += 1
        if self.calls == 1:
            return ChatResponse(content="unfinished constraint", finish_reason="length")
        return ChatResponse(content="keep the public API", finish_reason="stop")


async def test_truncated_summary_is_retried_without_installing_partial_content():
    provider = LengthProvider()
    compressor = ContextCompressor(provider)
    history = [ChatMessage(role="user", content="keep the public API" * 100)]
    summary = await compressor._summarize(history)
    assert summary == "keep the public API"
    assert "unfinished" not in summary
    assert provider.calls == 2


async def test_summary_retry_cannot_exceed_remaining_call_budget():
    provider = LengthProvider()
    compressor = ContextCompressor(provider)
    history = [ChatMessage(role="user", content="keep the public API" * 100)]
    assert await compressor._summarize(history, max_calls=1) == ""
    assert provider.calls == 1
    assert compressor.last_failure
