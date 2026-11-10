import json

import pytest

from examples.benchmark_scheduling import cases, run_batch, summarize
from xclaw.ai.types import ChatResponse


class FixedPlanner:
    def __init__(self, case, *, drop_dependencies=False):
        self.case = case
        self.drop_dependencies = drop_dependencies
        self.calls = 0

    async def chat(self, *args, **kwargs):
        self.calls += 1
        return ChatResponse(
            content=json.dumps(
                {
                    "mode": "dag",
                    "steps": [
                        {
                            "id": key,
                            "depends_on": []
                            if self.drop_dependencies
                            else self.case.dependencies.get(key, []),
                        }
                        for key, _ in self.case.calls
                    ],
                }
            ),
            usage={"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
        )


@pytest.mark.parametrize("case", cases(0.02, 0.01), ids=lambda case: case.id)
async def test_controlled_oracle_checks_actual_executor_and_serial_baseline(tmp_path, case):
    provider = FixedPlanner(case)
    serial = await run_batch(case, False, 1, tmp_path, provider, "fixture")
    assert provider.calls == 0
    dag = await run_batch(case, True, 1, tmp_path, provider, "fixture")
    assert dag["success"], dag
    assert serial["success"] is (not bool(case.fail))
    assert serial["peak_parallel"] == 1
    if case.id in {"four_fast_reads", "four_slow_reads", "eight_slow_reads", "fork_join"}:
        assert dag["peak_parallel"] >= 2
    if case.fail:
        assert dag["tool_skipped"] == 1
        assert serial["violations"][0]["kind"] == "failed_parent_executed"
    if case.id == "single_read":
        assert provider.calls == 0
    else:
        assert provider.calls == 1
    summary = summarize([serial, dag])
    assert summary["variants"]["serial"]["total_tokens"] == 0
    assert summary["variants"]["dag"]["usage_complete"]


async def test_valid_json_without_semantic_dependencies_does_not_get_credit(tmp_path):
    case = next(case for case in cases(0.02, 0.01) if case.id == "fork_join")
    row = await run_batch(
        case, True, 1, tmp_path, FixedPlanner(case, drop_dependencies=True), "fixture"
    )
    assert not row["success"]
    assert not row["checks"]["dependency_correct"]
    assert row["violations"]
