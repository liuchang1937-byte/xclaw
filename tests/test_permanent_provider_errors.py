import pytest

from xclaw.ai.client import AIClient
from xclaw.ai.context import ContextCompressor
from xclaw.ai.types import ChatMessage
from xclaw.evaluation.runner import EvalSettings, run_case
from xclaw.evaluation.tasks import builtin_tasks
from xclaw.telemetry import RunTrace, activate


class BalanceUnavailableError(Exception):
    status_code = 402


class Provider:
    def __init__(self):
        self.calls = 0

    async def chat(self, *args, **kwargs):
        self.calls += 1
        raise BalanceUnavailableError("test balance unavailable")


async def test_balance_error_is_not_retried_and_status_is_traced():
    provider = Provider()
    client = AIClient(provider)
    trace = RunTrace()
    with activate(trace), pytest.raises(BalanceUnavailableError):
        await client.chat([ChatMessage(role="user", content="hello")])
    assert provider.calls == 1
    event = next(e for e in trace.events if e["event"] == "model.finished")
    assert event["http_status"] == 402


async def test_summary_balance_failure_propagates_without_destroying_history():
    provider = Provider()
    compressor = ContextCompressor(provider, keep_recent=2)
    history = [
        ChatMessage(role="user", content="old" * 300),
        ChatMessage(role="assistant", content="old" * 300),
        ChatMessage(role="user", content="new"),
        ChatMessage(role="assistant", content="new"),
    ]
    with pytest.raises(BalanceUnavailableError):
        await compressor.compress(history)
    assert provider.calls == 1
    assert history[0].content == "old" * 300
    assert compressor.summary == ""


async def test_evaluator_classifies_provider_balance_failure_and_skips_validation(tmp_path):
    provider = Provider()

    async def forbidden_validator(*args):
        raise AssertionError("No validation should run after provider rejection")

    row = await run_case(
        builtin_tasks()[0],
        "summary_serial",
        1,
        tmp_path,
        EvalSettings(model="fake"),
        provider,
        validator=forbidden_validator,
    )
    assert row["status"] == "provider_balance_unavailable"
    assert row["http_status"] == 402
    assert row["success"] is False
    assert provider.calls == 1
