"""Check the probe's oracles independently of live model decisions."""

import json

import pytest

from examples.benchmark_dependencies import ancestors, run_case, scenarios, summarize
from xclaw.ai.types import ChatResponse


class FixturePlanner:
    def __init__(self, case, *, omit_dependencies=False, omit_defer=False):
        self.case = case
        self.omit_dependencies = omit_dependencies
        self.omit_defer = omit_defer
        self.requests = 0

    async def chat(self, *args, **kwargs):
        self.requests += 1
        return ChatResponse(
            content=json.dumps(
                {
                    "mode": "dag",
                    "steps": [
                        {
                            "id": c.id,
                            "depends_on": []
                            if self.omit_dependencies
                            else self.case.dependencies.get(c.id, []),
                            "defer": not self.omit_defer and c.id in self.case.deferred,
                        }
                        for c in self.case.calls
                    ],
                }
            ),
            usage={"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
        )


@pytest.mark.parametrize("case", scenarios(), ids=lambda c: c.id)
async def test_gold_plan_passes_real_file_tools_and_query_oracles(tmp_path, case):
    planner = FixturePlanner(case)
    row = await run_case(case, True, 1, tmp_path, planner, "fixture", delay=0.005)
    assert row["success"], row
    assert row["dependency_graph_complete"]
    assert planner.requests == 1
    assert row["metrics"]["usage_complete"]
    if case.independent_groups:
        assert row["overlapped_groups"] == len(case.independent_groups)


@pytest.mark.parametrize(
    "name",
    [
        "write_then_read_reversed",
        "two_edits_chain_reversed",
        "snapshot_update_snapshot",
        "query_fork_join",
        "transitive_failure_chain",
        "dynamic_record_id_deferred",
    ],
)
async def test_disabled_scheduler_exposes_order_or_failure_or_parameter_contract(tmp_path, name):
    case = next(c for c in scenarios() if c.id == name)
    planner = FixturePlanner(case)
    row = await run_case(case, False, 1, tmp_path, planner, "fixture", delay=0)
    assert not row["execution_success"]
    assert planner.requests == 0
    assert row["peak_parallel"] == 1


@pytest.mark.parametrize(
    "name",
    [
        "write_then_read_reversed",
        "query_fork_join",
        "two_edits_chain_reversed",
    ],
)
async def test_missing_semantic_edges_fail_even_with_valid_plan_json(tmp_path, name):
    case = next(c for c in scenarios() if c.id == name)
    row = await run_case(
        case, True, 1, tmp_path, FixturePlanner(case, omit_dependencies=True), "fixture", delay=0
    )
    assert not row["execution_success"]
    assert not row["dependency_graph_complete"]
    assert row["violations"]


async def test_omitted_defer_executes_guessed_id_and_is_rejected(tmp_path):
    case = next(c for c in scenarios() if c.id == "dynamic_record_id_deferred")
    row = await run_case(
        case, True, 1, tmp_path, FixturePlanner(case, omit_defer=True), "fixture", delay=0
    )
    assert not row["success"]
    assert not row["checks"]["expected_execution"]
    assert not row["checks"]["blocked_or_deferred"]


def test_transitive_dependencies_and_summary_denominators():
    plan = {
        "steps": [
            {"id": "c", "depends_on": ["b"]},
            {"id": "b", "depends_on": ["a"]},
            {"id": "a", "depends_on": []},
        ]
    }
    assert ancestors(plan)["c"] == {"a", "b"}
    case_ids = [c.id for c in scenarios()]
    assert len(case_ids) == len(set(case_ids)) == 24
    assert summarize([]) == {"variants": {}, "by_case": {}}
