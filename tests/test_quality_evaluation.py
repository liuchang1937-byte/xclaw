import json
from copy import deepcopy

import pytest

from tests.test_evaluation import FixProvider, trusted_fixture_validation
from xclaw.ai.client import AIClient
from xclaw.ai.provider import OpenAIProvider
from xclaw.ai.types import ChatMessage, ChatResponse
from xclaw.evaluation.context_probe import CASES, run_probe
from xclaw.evaluation.evidence import EvidencePacket, build_case_packet
from xclaw.evaluation.judge import DIMENSIONS, evaluate, grade_summary, parse_grade
from xclaw.evaluation.pipeline import run_quality_case
from xclaw.evaluation.quality_metrics import trajectory_metrics
from xclaw.evaluation.quality_report import calibrate, compare_records
from xclaw.evaluation.runner import EvalSettings, run_case
from xclaw.evaluation.score_cli import select_rows
from xclaw.evaluation.tasks import builtin_tasks
from xclaw.telemetry import RunTrace, model_purpose


def packet(subject="coding_task"):
    return EvidencePacket(
        subject,
        {
            "requirements": "r1: keep the API",
            "answer": "API is preserved",
            "execution": "tests failed",
        },
        ["r1"],
        {"success": False, "validation_passed": False},
        {},
        [],
        {"case_id": "case", "variant": "a", "task_id": "x", "attempt": 1, "task_hash": "pinned"},
    )


def grade_data():
    score = {
        "score": 4,
        "reason": "candidate states this",
        "evidence": [{"source": "answer", "quote": "API is preserved"}],
    }
    return {
        "schema_version": "1",
        "dimensions": {d: deepcopy(score) for d in DIMENSIONS},
        "requirements": [
            {
                "id": "r1",
                "satisfied": True,
                "reason": "candidate evidence",
                "evidence": [{"source": "answer", "quote": "API is preserved"}],
            }
        ],
        "recommendations": ["Check the failed tests before claiming completion"],
    }


@pytest.mark.parametrize("mutation", ["score", "quote", "id", "bool_score"])
def test_judge_rejects_out_of_range_scores_and_fabricated_evidence(mutation):
    data = grade_data()
    if mutation == "score":
        data["dimensions"]["accuracy"]["score"] = 6
    elif mutation == "quote":
        data["dimensions"]["accuracy"]["evidence"][0]["quote"] = "not in any source"
    elif mutation == "id":
        data["requirements"][0]["id"] = "invented"
    else:
        data["dimensions"]["accuracy"]["score"] = True
    with pytest.raises(ValueError):
        parse_grade(json.dumps(data), packet())


def test_high_judge_score_cannot_override_failed_objective_validation():
    p = packet()
    result = grade_summary(parse_grade(json.dumps(grade_data()), p), p)
    assert result["objective"]["success"] is False
    assert "judge_conflicts_with_tests" in result["human_review_flags"]


def test_quote_repair_feedback_identifies_actual_source_without_relaxing_validation():
    data = grade_data()
    data["dimensions"]["accuracy"]["evidence"][0]["quote"] = "API-is-preserved"
    with pytest.raises(ValueError) as error:
        parse_grade(json.dumps(data), packet())
    assert "API-is-preserved" in str(error.value)
    assert "API is preserved" in str(error.value)


def test_context_retention_needs_quote_from_retained_context_not_reference():
    p, data = packet("context"), grade_data()
    data["requirements"][0]["evidence"] = [{"source": "requirements", "quote": "keep the API"}]
    with pytest.raises(ValueError, match="candidate evidence"):
        parse_grade(json.dumps(data), p)


async def test_invalid_judgment_has_one_bounded_retry_and_separate_usage():
    class Provider:
        calls = 0

        async def chat(self, *args, **kwargs):
            self.calls += 1
            if self.calls == 2:
                assert args[0][-2].role == "assistant"
                assert args[0][-2].content == "{}"
                assert "missing" in args[0][-1].content
            return ChatResponse(
                content="{}" if self.calls == 1 else json.dumps(grade_data()),
                usage={"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
            )

    provider = Provider()
    result = await evaluate(packet(), provider, model="fake")
    assert result["status"] == "graded"
    assert provider.calls == 2
    assert result["metrics"]["reported_tokens"] == 60
    assert result["metrics"]["calls_by_purpose"] == {"judge": 2}
    assert len(result["rejected_responses"]) == 1
    assert result["rejected_responses"][0]["content"] == "{}"
    assert "missing" in result["rejected_responses"][0]["validation_error"]


async def test_judge_budget_failure_happens_before_provider_call():
    class Provider:
        async def chat(self, *args, **kwargs):
            raise AssertionError("oversized evidence must not reach provider")

    with pytest.raises(ValueError, match="input budget"):
        await evaluate(packet(), Provider(), model="fake", input_budget=1)


def test_judge_json_mode_and_thinking_are_scoped_to_judging():
    provider = object.__new__(OpenAIProvider)
    provider.auxiliary_thinking, provider.judge_json_mode = False, True
    with model_purpose("judge"):
        kwargs = provider._build_request_kwargs([], None, "deepseek-v4-pro", 0, 2048)
    assert kwargs["response_format"] == {"type": "json_object"}
    assert kwargs["extra_body"] == {"thinking": {"type": "disabled"}}
    with model_purpose("main"):
        kwargs = provider._build_request_kwargs([], None, "deepseek-v4-pro", 0, 2048)
    assert "response_format" not in kwargs and "extra_body" not in kwargs


def test_private_fingerprints_match_only_within_run_and_canonicalize_json():
    first, second = RunTrace(), RunTrace()
    a = first.argument_fingerprint("read", '{"path":"private","offset":1}')
    assert a == first.argument_fingerprint("read", '{ "offset": 1, "path": "private" }')
    assert a != second.argument_fingerprint("read", '{"path":"private","offset":1}')
    assert "private" not in a


def test_legacy_traces_do_not_fabricate_repeated_call_or_context_metrics():
    events = [
        {"event": "tool.started", "tool": "read", "call_id": "a"},
        {"event": "tool.finished", "tool": "read", "status": "ok", "call_id": "a"},
    ]
    result = trajectory_metrics(events, [["read"], ["write"]])
    assert result["tool_success_rate"] == 1
    assert result["expected_tool_group_coverage"] == 0.5
    assert result["repeated_argument_call_ratio"] is None
    assert result["estimated_context_reduction_ratio"] is None


def test_repeated_argument_rate_is_not_repeated_tool_name_rate():
    events = [
        {"event": "tool.started", "tool": "read", "argument_fingerprint": v}
        for v in ("a", "b", "a")
    ]
    assert trajectory_metrics(events)["repeated_argument_call_ratio"] == pytest.approx(1 / 3)


async def test_evidence_integration_keeps_reference_and_variant_out_of_judge_payload(tmp_path):
    async def validator(*args):
        result = await trusted_fixture_validation(*args)
        return {**result, "stdout": "OK\n", "stderr": "Ran 4 tests\n\nOK\n"}

    task = builtin_tasks()[0]
    row = await run_case(
        task,
        "summary_serial",
        1,
        tmp_path,
        EvalSettings(model="fake"),
        FixProvider(task),
        validator=validator,
    )
    p = build_case_packet(row, tmp_path)
    payload = p.payload()
    assert "summary_serial" not in json.dumps(payload)
    assert "reference" not in payload
    assert "keep" not in payload  # no ad-hoc invented references
    assert p.objective["success"] is True
    assert p.components["tool_success_rate"] == 1
    assert "Ran 4 tests\n\nOK" in p.sources["execution"]
    assert "Ran 4 tests\\n\\nOK" not in p.sources["execution"]


async def test_pipeline_runs_agent_and_separate_judge_without_exposing_tools(tmp_path):
    class EvidenceJudge:
        async def chat(self, messages, tools, **kwargs):
            assert tools is None
            payload = json.loads(messages[1].content)
            assert "variant" not in payload
            sources = payload["sources"]
            citation = {"source": "answer", "quote": sources["answer"][:40]}
            data = {
                "schema_version": "1",
                "dimensions": {
                    d: {"score": 4, "reason": "test fixture", "evidence": [citation]}
                    for d in DIMENSIONS
                },
                "requirements": [
                    {"id": r, "satisfied": None, "reason": "needs review", "evidence": [citation]}
                    for r in payload["requirement_ids"]
                ],
                "recommendations": [],
            }
            return ChatResponse(content=json.dumps(data))

    task = builtin_tasks()[0]
    result = await run_quality_case(
        task,
        "summary_serial",
        1,
        tmp_path,
        EvalSettings(model="fake"),
        FixProvider(task),
        EvidenceJudge(),
        judge_model="fixture-judge",
        validator=trusted_fixture_validation,
    )
    assert result["objective"]["success"] is True
    assert result["quality"]["status"] == "graded"
    assert result["quality"]["metrics"]["calls_by_purpose"] == {"judge": 1}
    assert (tmp_path / result["quality"]["metadata"]["case_id"] / "quality.json").exists()


async def test_context_probe_measures_actual_prepared_input_and_preserves_artifacts(tmp_path):
    class NoModel:
        async def chat(self, *args, **kwargs):
            raise AssertionError("truncation probe must not call a model")

    row = await run_probe(
        "money", CASES["money"], "truncate", NoModel(), "fake", tmp_path / "probe"
    )
    assert row["status"] == "prepared"
    assert row["fits_estimated_budget"] is True
    sent = json.loads((tmp_path / "probe" / "prepared_messages.json").read_text(encoding="utf-8"))
    assert row["current_user_input_preserved"] is True
    assert sent[-1]["content"] == "继续工作，遵守此前项目约束。"
    assert row["metrics"]["model_calls"] == 0
    assert "semantic_constraint_retention_ratio" not in row


def test_human_calibration_unknown_is_not_perfect_agreement():
    result = calibrate([], [{"case_id": "case", "scores": {d: None for d in DIMENSIONS}}])
    assert all(v["agreement"] is None for v in result.values())


def test_calibration_matches_known_ordinal_example():
    records = [
        {"status": "graded", "metadata": {"case_id": str(i)}, "scores": {"accuracy": score}}
        for i, score in enumerate((1, 2, 5))
    ]
    labels = [
        {"case_id": str(i), "scores": {"accuracy": score}} for i, score in enumerate((1, 3, 5))
    ]
    result = calibrate(records, labels)["accuracy"]
    assert result["agreement"] == pytest.approx(2 / 3)
    assert result["mae"] == pytest.approx(1 / 3)
    assert result["quadratic_kappa"] == pytest.approx(16 / 17)


def test_regression_does_not_compare_different_task_baselines():
    base = {
        "status": "graded",
        "metadata": {"task_id": "x", "attempt": 1, "task_hash": "a"},
        "objective": {"success": True},
        "scores": {d: 5 for d in DIMENSIONS},
    }
    changed = deepcopy(base)
    changed["metadata"]["task_hash"] = "b"
    changed["objective"]["success"] = False
    result = compare_records([base], [changed])
    assert result["objective_regressions"] == []
    assert result["incomparable_judgments"]


def test_missing_judge_provenance_cannot_be_used_for_quality_comparison():
    record = {
        "status": "graded",
        "metadata": {"task_id": "x", "attempt": 1, "task_hash": "same"},
        "objective": {"success": True},
        "scores": {d: 5 for d in DIMENSIONS},
    }
    result = compare_records([record], [deepcopy(record)])
    assert result["dimension_compared_counts"]["accuracy"] == 0
    assert result["incomparable_judgments"]


def test_stratified_selection_includes_failures_and_successes():
    rows = [
        {"category": category, "success": success, "task_id": str(i), "variant": "a", "attempt": 1}
        for i, (category, success) in enumerate(("bug", True) for _ in range(5))
    ]
    rows += [
        {"category": "bug", "success": False, "task_id": "z", "variant": "a", "attempt": 1},
        {"category": "context", "success": False, "task_id": "y", "variant": "a", "attempt": 1},
    ]
    selected = select_rows(rows, 3)
    assert {r["success"] for r in selected} == {True, False}
    assert {r["category"] for r in selected} == {"bug", "context"}


async def test_truncation_stops_dropping_history_once_budget_fits():
    class Provider:
        async def chat(self, *args, **kwargs):
            raise AssertionError("truncate baseline must not call a summary model")

    client = AIClient(Provider(), system_prompt="short", max_tokens=64)
    client.enable_compression(context_window=1024, safety_margin=32, keep_recent=2)
    client.context_strategy = "truncate"
    client.set_history(
        [
            ChatMessage(role="user", content="old " * 3000),
            ChatMessage(role="assistant", content="ack"),
            ChatMessage(role="user", content="recent constraint"),
            ChatMessage(role="assistant", content="recent reply"),
        ]
    )
    sent = await client._prepare_messages([ChatMessage(role="user", content="current")], None)
    assert any(m.content == "recent constraint" for m in sent)
    assert sent[-1].content == "current"
