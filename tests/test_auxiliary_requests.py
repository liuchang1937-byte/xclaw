import asyncio

from xclaw.ai.provider import OpenAIProvider
from xclaw.evaluation.runner import execute_jobs
from xclaw.telemetry import model_purpose


def test_deepseek_auxiliary_calls_disable_thinking_without_changing_main_requests():
    provider = object.__new__(OpenAIProvider)
    provider.auxiliary_thinking = False
    with model_purpose("main"):
        assert "extra_body" not in provider._build_request_kwargs(
            [], None, "deepseek-v4-pro", 0, 1024
        )
    for purpose in ("compression", "scheduling", "memory"):
        with model_purpose(purpose):
            request = provider._build_request_kwargs([], None, "deepseek-v4-pro", 0, 1024)
            assert request["extra_body"] == {"thinking": {"type": "disabled"}}
            assert "extra_body" not in provider._build_request_kwargs(
                [], None, "other-model", 0, 1024
            )
    provider.auxiliary_thinking = None
    with model_purpose("compression"):
        assert "extra_body" not in provider._build_request_kwargs(
            [], None, "deepseek-v4-pro", 0, 1024
        )


async def test_evaluation_workers_are_bounded_and_do_not_drop_jobs():
    active = peak = 0
    finished = []
    barrier = asyncio.Event()

    async def execute(index):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        if active == 2:
            barrier.set()
        await asyncio.wait_for(barrier.wait(), 1)
        await asyncio.sleep(0)
        finished.append(index)
        active -= 1

    await execute_jobs([(i,) for i in range(9)], execute, 2)
    assert sorted(finished) == list(range(9))
    assert peak == 2
