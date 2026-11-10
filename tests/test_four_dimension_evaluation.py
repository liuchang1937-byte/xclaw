import json
from collections import Counter
from copy import deepcopy

import pytest

from tests.test_evaluation import FixProvider, trusted_fixture_validation
from xclaw.ai.types import ChatResponse, ToolCall
from xclaw.evaluation.assessment import (
    assess_case,
    attach_assessment,
    failure_attribution,
    summarize_dimensions,
)
from xclaw.evaluation.audit import check_gate, export_review_pairs, import_feedback, replay
from xclaw.evaluation.datasets import balanced_tasks, select_tasks
from xclaw.evaluation.runner import (
    EvalSettings,
    run_case,
    self_check,
    source_versions,
    write_report,
)
from xclaw.evaluation.spec import EvaluationSpec, evaluation_hash
from xclaw.evaluation.tasks import builtin_tasks
from xclaw.persistence import atomic_json
from xclaw.telemetry import RunTrace, current_model_purpose


def trace(tools=("read_file", "write_file"), *, error=False):
    events = [{"event": "model.started", "purpose": "main"}]
    for i, tool in enumerate(tools):
        events.extend(
            [
                {
                    "event": "tool.started",
                    "tool": tool,
                    "call_id": str(i),
                    "argument_fingerprint": f"{tool}-{i}",
                },
                {
                    "event": "tool.finished",
                    "tool": tool,
                    "call_id": str(i),
                    "status": "error" if i == 0 and error else "ok",
                    "error_category": "parameter" if i == 0 and error else None,
                },
            ]
        )
    events += [{"event": "model.started", "purpose": "main"}, {"event": "run.finished"}]
    return events


def row(task_id="x", *, success=True, attempt=1):
    return {
        "task_id": task_id,
        "category": "bug",
        "variant": "a",
        "attempt": attempt,
        "success": success,
        "status": "completed" if success else "acceptance_failed",
        "duration_seconds": 2,
        "metrics": {"reported_tokens": 20, "usage_complete": True},
    }


def test_balanced_dataset_is_versioned_stratified_and_fixture_checked(tmp_path):
    tasks = balanced_tasks()
    assert Counter(t.evaluation["scenario"] for t in tasks) == {
        "basic": 12,
        "boundary": 4,
        "adversarial": 2,
        "regression": 2,
    }
    assert len(select_tasks(tasks, mode="smoke")) == 3
    assert all(t.evaluation["badcase"] for t in tasks if t.evaluation["scenario"] == "regression")
    assert self_check(tmp_path, tasks)["passed"]
    with pytest.raises(ValueError, match="没有用例"):
        select_tasks(tasks, split="holdout")


@pytest.mark.parametrize(
    "data",
    [
        {"scenario": "made_up"},
        {"critical": "yes"},
        {"expected_tool_sequence": [[]]},
        {"rules": [{"id": "x", "kind": "max_steps", "dimension": "efficiency", "limit": True}]},
        {"rules": [{"id": "x", "kind": "answer_contains", "dimension": "correctness"}]},
    ],
)
def test_bad_annotations_are_rejected_before_running(data):
    with pytest.raises(ValueError):
        EvaluationSpec.model_validate(data)


def test_success_does_not_hide_bad_process_and_no_selection_oracle_means_unknown():
    result = assess_case(row(), EvaluationSpec(), trace(error=True), "done")
    assert result["success"]
    assert result["process_quality"]["invalid_step_ratio"] == 0.5
    assert result["process_quality"]["tool_selection_accuracy"] is None
    assert result["effectiveness"]["user_satisfaction"] is None
    assert result["efficiency"]["steps"] == 2


def test_selection_oracle_penalizes_wrong_tool_missing_and_extra_calls():
    spec = EvaluationSpec(expected_tool_sequence=[["read_file"], ["write_file", "edit_file"]])
    result = assess_case(row(), spec, trace(("bash", "write_file", "read_file")), "done")
    assert result["process_quality"]["tool_selection_accuracy"] == pytest.approx(1 / 3)
    result = assess_case(row(), spec, trace(("read_file",)), "done")
    assert result["process_quality"]["tool_selection_accuracy"] == 0.5


def test_missing_trace_and_missing_arguments_never_count_as_perfect():
    spec = EvaluationSpec.model_validate(
        {
            "rules": [
                {
                    "id": "path",
                    "kind": "tool_argument",
                    "dimension": "process",
                    "call_index": 0,
                    "arguments": {"path": "app.py"},
                }
            ]
        }
    )
    result = attach_assessment(row(), spec, trace(), "done")
    assert result["status"] == "evaluation_unavailable"
    assert result["failure_attribution"]["category"] == "evaluator"
    result = assess_case(row(), EvaluationSpec(), [], None)
    assert result["efficiency"]["steps"] is None
    assert result["process_quality"]["invalid_step_ratio"] is None


def test_hard_rule_failure_overrides_artifact_acceptance_with_rubric_evidence():
    spec = EvaluationSpec.model_validate(
        {
            "rules": [
                {
                    "id": "confirmation",
                    "kind": "answer_contains",
                    "text": "confirmed",
                    "dimension": "safety",
                }
            ]
        }
    )
    result = attach_assessment(row(), spec, trace(), "done")
    assert result["success"] is False
    assert result["assessment"]["objective_success"] is True
    assert result["assessment"]["rubric"]["safety"] == {"score": 1, "checks": ["confirmation"]}
    assert result["assessment"]["rubric"]["evidence"]["score"] is None


def test_arguments_and_sequence_rules_use_actual_captured_evidence():
    spec = EvaluationSpec.model_validate(
        {
            "rules": [
                {
                    "id": "sequence",
                    "kind": "tool_sequence",
                    "dimension": "process",
                    "sequence": [["read_file"], ["write_file"]],
                },
                {
                    "id": "path",
                    "kind": "tool_argument",
                    "dimension": "process",
                    "call_index": 0,
                    "arguments": {"path": "app.py"},
                },
                {"id": "steps", "kind": "max_steps", "dimension": "efficiency", "limit": 1},
            ]
        }
    )
    events = trace() + [
        {
            "event": "tool.arguments",
            "payload": {
                "call_id": "0",
                "arguments": '{"path":"app.py","offset":1}',
            },
        }
    ]
    result = assess_case(row(), spec, events, "done")
    assert [r["passed"] for r in result["rules"]] == [True, True, False]


def test_robustness_requires_eligible_cases_and_observed_errors():
    spec = EvaluationSpec(fault_recovery=True, scenario="adversarial", long_task=True)
    result = assess_case(row(), spec, trace(error=True), "done")
    assert result["robustness"]["exception_recovered"] is True
    assert result["robustness"]["adversarial_passed"] is True
    result = assess_case(row(), spec, trace(), "done")
    assert result["robustness"]["exception_recovered"] is None


def test_task_weighting_prevents_extra_repeats_from_inflating_success():
    rows = [row("easy", attempt=i) for i in range(1, 4)] + [row("hard", success=False)]
    for r in rows:
        attach_assessment(r, EvaluationSpec(), trace(), "done")
    result = summarize_dimensions(rows)["a"]["dimensions"]["effectiveness"]
    assert result["success_rate"] == 0.5
    assert result["attempt_success_rate"] == 0.75
    assert result["user_satisfaction"]["value"] is None
    assert result["user_satisfaction"]["coverage"] == 0


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("timeout", "timeout"),
        ("validation_error", "evaluator"),
        ("round_limit", "planning"),
        ("acceptance_failed", "execution"),
    ],
)
def test_failure_stage_does_not_pretend_to_prove_root_cause(status, expected):
    r = row(success=False)
    r["status"] = status
    result = failure_attribution(r)
    assert result["category"] == expected
    assert result["root_cause_confirmed"] is False


def test_capture_is_opt_in_and_oversize_payload_is_explicitly_unknown():
    default = RunTrace()
    default.capture("request", secret="hidden")
    assert default.events == []
    captured = RunTrace(capture_content=True, content_limit=10)
    captured.capture("request", text="too large")
    assert captured.events[-1]["truncated"] is True
    assert captured.events[-1]["payload"] is None


def write_run(root, *, success=True, critical=False):
    spec = EvaluationSpec(critical=critical)
    r = row(success=success)
    attach_assessment(r, spec, trace(), "done")
    r.update(
        {
            "task_hash": "task",
            "evaluation_hash": evaluation_hash(spec),
            "suite": {"id": "fixed", "kind": "product_regression", "version": "1"},
            "settings": {"model": "pinned", "max_rounds": 4, "max_tokens": 100},
            "provenance": {
                "model": "pinned",
                "prompt_hash": "prompt",
                "tools_hash": "tools",
                "python": "3.13",
                "platform": "test",
                "image": "sha256:fixed",
                "dependencies": {"openai": "fixed"},
                "sources": {
                    "runtime": "runtime",
                    "tools": "tools",
                    "scorer": "scorer",
                    "prompts": "prompts",
                },
            },
        }
    )
    meta = {
        "run_status": "completed",
        "selection": "full",
        "split": "evaluation",
        "dataset_hash": "dataset",
        "evaluation_hash": "evaluation",
        "suite": r["suite"],
        "scorer_version": "v1",
        "concurrency": 1,
        "tasks": ["x"],
        "variants": ["a"],
        "repeats": 1,
    }
    atomic_json(root / "results.json", [r])
    atomic_json(root / "experiment.json", meta)
    return r, meta


def test_gate_reports_cost_increase_and_blocks_regressed_critical_task(tmp_path):
    baseline, candidate = tmp_path / "before", tmp_path / "after"
    write_run(baseline, critical=True)
    write_run(candidate, critical=True, success=False)
    result = check_gate(baseline, candidate, min_success=0)
    assert result["passed"] is False
    assert result["regressions"]
    assert any("critical" in b for b in result["blockers"])


@pytest.mark.parametrize("mutation", ["image", "budget", "model", "scorer", "missing", "smoke"])
def test_gate_fails_closed_for_changed_environment_and_incomplete_runs(tmp_path, mutation):
    baseline, candidate = tmp_path / "before", tmp_path / "after"
    write_run(baseline)
    r, meta = write_run(candidate)
    if mutation == "budget":
        r["settings"]["max_tokens"] = 200
    elif mutation == "smoke":
        meta["selection"] = "smoke"
    elif mutation == "missing":
        meta["repeats"] = 2
    elif mutation == "scorer":
        r["provenance"]["sources"]["scorer"] = "changed"
    else:
        r["provenance"][mutation] = "changed"
    atomic_json(candidate / "results.json", [r])
    atomic_json(candidate / "experiment.json", meta)
    assert check_gate(baseline, candidate)["passed"] is False


def test_gate_allows_declared_model_change_with_fixed_budget(tmp_path):
    baseline, candidate = tmp_path / "before", tmp_path / "after"
    write_run(baseline)
    r, _ = write_run(candidate)
    r["provenance"]["model"] = r["settings"]["model"] = "new-model"
    atomic_json(candidate / "results.json", [r])
    assert check_gate(baseline, candidate, change="model")["passed"]


def test_feedback_requires_independent_observations(tmp_path):
    write_run(tmp_path)
    labels = tmp_path / "labels.json"
    label = {
        "task_id": "x",
        "attempt": 1,
        "variant": "a",
        "score": 4,
        "source": "user",
        "evidence": "support-ticket-123",
    }
    atomic_json(labels, [label])
    result = import_feedback(tmp_path, labels)
    satisfaction = result["dimensions"]["a"]["dimensions"]["effectiveness"]["user_satisfaction"]
    assert satisfaction["value"] == 4
    label["source"] = "agent"
    atomic_json(labels, [label])
    with pytest.raises(ValueError, match="independent"):
        import_feedback(tmp_path, labels)


async def test_real_loop_captures_evidence_replays_and_hard_rules_cannot_be_bypassed(tmp_path):
    task = deepcopy(builtin_tasks()[0])
    task.evaluation = {
        "rules": [
            {
                "id": "reply",
                "kind": "answer_contains",
                "text": "CONFIRMED",
                "dimension": "safety",
            }
        ]
    }
    result = await run_case(
        task,
        "summary_serial",
        1,
        tmp_path,
        EvalSettings(model="fixture"),
        FixProvider(task),
        validator=trusted_fixture_validation,
    )
    assert result["validation_passed"] is True
    assert result["success"] is False
    assert result["status"] == "evaluation_rule_failed"
    case = tmp_path / "clamp-summary_serial-1"
    events = [
        json.loads(line)
        for p in (case / "traces").glob("*.jsonl")
        for line in p.read_text(encoding="utf-8").splitlines()
    ]
    assert {
        "model.request",
        "model.response",
        "tool.arguments",
        "tool.result",
        "evaluation.finished",
    } <= {e["event"] for e in events}
    atomic_json(tmp_path / "results.json", [result])
    replayed = replay(tmp_path)
    assert replayed["model_called"] is False
    assert replayed["rows"][0]["assessment"] == result["assessment"]


def test_prompt_version_changes_do_not_masquerade_as_runtime_changes(tmp_path):
    folder = tmp_path / "coding"
    folder.mkdir()
    source = folder / "coding_agent.py"
    source.write_text("class Agent:\n    SYSTEM_PROMPT='old'\n    version=1\n")
    original = source_versions(tmp_path)
    source.write_text("class Agent:\n    SYSTEM_PROMPT='new'\n    version=1\n")
    changed = source_versions(tmp_path)
    assert original["runtime"] == changed["runtime"]
    assert original["prompts"] != changed["prompts"]
    assert original["raw"] != changed["raw"]
    source.write_text("class Agent:\n    SYSTEM_PROMPT='new'\n    version=2\n")
    assert source_versions(tmp_path)["runtime"] != changed["runtime"]


def test_gate_blocks_expensive_success_and_unknown_token_cost(tmp_path):
    baseline, candidate = tmp_path / "before", tmp_path / "after"
    write_run(baseline)
    r, _ = write_run(candidate)
    r["assessment"]["efficiency"]["reported_tokens"] = 40
    atomic_json(candidate / "results.json", [r])
    result = check_gate(baseline, candidate, max_cost_increase=0.2)
    assert not result["passed"]
    assert result["variants"]["a"]["efficiency"]["tokens"]["relative_change"] == 1
    r["metrics"]["usage_complete"] = False
    atomic_json(candidate / "results.json", [r])
    assert any(
        "coverage incomplete" in b
        for b in check_gate(
            baseline,
            candidate,
            max_cost_increase=0.2,
        )["blockers"]
    )


def test_blind_pairs_hide_variant_labels_and_keep_private_key_separate(tmp_path, monkeypatch):
    baseline, candidate, output = tmp_path / "before", tmp_path / "after", tmp_path / "review"
    write_run(baseline)
    write_run(candidate)

    class Evidence:
        def payload(self):
            return {"sources": {"answer": "done"}}

    monkeypatch.setattr("xclaw.evaluation.audit.build_case_packet", lambda *args: Evidence())
    result = export_review_pairs(
        baseline,
        candidate,
        output,
        baseline_variant="a",
        candidate_variant="a",
    )
    assert len(result["pairs"]) == 1
    assert "origin" not in (output / "review.json").read_text()
    assert "baseline" in (output / "private_key.json").read_text()


@pytest.mark.parametrize("task", balanced_tasks(), ids=lambda task: task.id)
async def test_balanced_suite_uses_real_agent_and_deterministic_harness_without_live_model(
    tmp_path, task
):
    # Curated fixture references only; this is harness validation, never a benchmark score.
    class ReferenceProvider:
        stage = 0

        async def chat(self, messages, tools=None, **kwargs):
            if current_model_purpose() == "compression":
                return ChatResponse(content=task.history[0]["content"])
            if self.stage == 0 and task.id in {"tool_read_then_patch", "missing_parameter"}:
                self.stage += 1
                path = "app.py" if task.id == "tool_read_then_patch" else "optional-config.json"
                return ChatResponse(
                    tool_calls=[
                        ToolCall(
                            id="read",
                            function={
                                "name": "read_file",
                                "arguments": json.dumps({"path": path}),
                            },
                        )
                    ]
                )
            if self.stage < 2 and task.reference_answer is None:
                self.stage = 2
                return ChatResponse(
                    tool_calls=[
                        ToolCall(
                            id=f"write-{i}",
                            function={
                                "name": "write_file",
                                "arguments": json.dumps({"path": path, "content": text}),
                            },
                        )
                        for i, (path, text) in enumerate(task.reference.items())
                    ]
                )
            return ChatResponse(content=task.reference_answer or "ADD_FIXED DOUBLE_FIXED FIXED")

    result = await run_case(
        task,
        "summary_serial",
        1,
        tmp_path,
        EvalSettings(model="reference-fixture"),
        ReferenceProvider(),
        validator=trusted_fixture_validation,
    )
    assert result["success"], result
    if task.id == "tool_read_then_patch":
        assert result["assessment"]["process_quality"]["tool_selection_accuracy"] == 1
    if task.id == "missing_parameter":
        assert result["assessment"]["robustness"]["exception_recovered"] is True
    write_report(tmp_path, [result])
    assert (tmp_path / "dimensions.json").is_file()
