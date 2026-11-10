"""Deterministic four-dimensional assessment with explicit evidence coverage."""

from __future__ import annotations

import json
import statistics
from collections import Counter, defaultdict

from xclaw.evaluation.quality_metrics import trajectory_metrics
from xclaw.evaluation.spec import (
    RUBRIC_DIMENSIONS,
    SCENARIOS,
    SCORER_VERSION,
    EvaluationSpec,
    evaluation_hash,
)


def _tool_arguments(events):
    result = {}
    for event in events:
        if event.get("event") != "tool.arguments" or not event.get("payload"):
            continue
        payload = event["payload"]
        try:
            result[payload["call_id"]] = json.loads(payload["arguments"] or "{}")
        except (ValueError, TypeError):
            result[payload["call_id"]] = None
    return result


def assess_case(row: dict, spec: EvaluationSpec, events: list[dict], answer: str | None) -> dict:
    starts = [e for e in events if e["event"] == "tool.started"]
    finishes = [e for e in events if e["event"] == "tool.finished"]
    skipped = [e for e in events if e["event"] == "tool.skipped"]
    complete = any(e["event"] == "run.finished" for e in events)
    complete = complete and not row.get("metrics", {}).get("trace_write_failed", False)
    names = [e["tool"] for e in starts]
    arguments = _tool_arguments(events)
    steps = (
        sum(e["event"] == "model.started" and e.get("purpose") == "main" for e in events)
        if complete
        else None
    )
    rule_results = []
    for rule in spec.rules:
        passed = None
        if rule.kind == "answer_contains" and answer is not None:
            passed = rule.text in answer
        elif rule.kind == "answer_excludes" and answer is not None:
            passed = rule.text not in answer
        elif rule.kind == "tool_sequence" and complete:
            passed = len(names) == len(rule.sequence) and all(
                name in group for name, group in zip(names, rule.sequence)
            )
        elif rule.kind == "tool_argument" and complete:
            if rule.call_index >= len(starts):
                passed = False
            else:
                args = arguments.get(starts[rule.call_index]["call_id"])
                if isinstance(args, dict):
                    passed = all(
                        key in args and args[key] == value for key, value in rule.arguments.items()
                    )
        elif rule.kind == "max_steps" and steps is not None:
            passed = steps <= rule.limit
        rule_results.append(
            {
                "id": rule.id,
                "dimension": rule.dimension,
                "hard": rule.hard,
                "passed": passed,
                "oracle": rule.kind,
            }
        )
    annotated = spec.expected_tool_sequence
    selection_total = max(len(names), len(annotated)) if annotated else 0
    selection_correct = sum(name in group for name, group in zip(names, annotated or []))
    proposals = len(starts) + len(skipped)
    finished_all = complete and len(starts) == len(finishes)
    failed_calls = {e.get("call_id") for e in finishes if e["status"] == "error"}
    invalid = sum(
        e.get("call_id") in failed_calls
        or (annotated is not None and (i >= len(annotated) or e["tool"] not in annotated[i]))
        for i, e in enumerate(starts)
    ) + len(skipped)
    answer_rules = [
        r
        for r in rule_results
        if r["oracle"].startswith("answer_") and r["dimension"] == "correctness"
    ]
    observed_answers = [r["passed"] for r in answer_rules if r["passed"] is not None]
    hard_passed = all(r["passed"] is True for r in rule_results if r["hard"])
    final_success = row["success"] is True and hard_passed
    components = trajectory_metrics(events)
    faults = sum(e["status"] == "error" for e in finishes) + sum(
        e["event"] == "model.finished" and e.get("status") == "error" for e in events
    )
    rubric = {}
    for dimension in RUBRIC_DIMENSIONS:
        checks = [r for r in rule_results if r["dimension"] == dimension]
        rubric[dimension] = {
            "score": (
                None
                if not checks or any(r["passed"] is None for r in checks)
                else 1
                if any(r["hard"] and not r["passed"] for r in checks)
                else 5
                if all(r["passed"] for r in checks)
                else 3
                if any(r["passed"] for r in checks)
                else 1
            ),
            "checks": [r["id"] for r in checks],
        }
    return {
        "scorer_version": SCORER_VERSION,
        "contract_hash": evaluation_hash(spec),
        "spec": spec.model_dump(),
        "objective_success": row["success"],
        "success": final_success,
        "hard_rules_passed": hard_passed,
        "rules": rule_results,
        "rubric": rubric,
        "effectiveness": {
            "success": final_success,
            "answer_accuracy": (
                sum(observed_answers) / len(observed_answers)
                if answer_rules and len(observed_answers) == len(answer_rules)
                else None
            ),
            "user_satisfaction": None,
        },
        "efficiency": {
            "steps": steps,
            "tool_calls": len(starts) if complete else None,
            "reported_tokens": row.get("metrics", {}).get("reported_tokens"),
            "usage_complete": row.get("metrics", {}).get("usage_complete", False),
            "latency_seconds": row.get("duration_seconds"),
        },
        "robustness": {
            "exception_recovered": final_success if spec.fault_recovery and faults else None,
            "adversarial_passed": final_success if spec.scenario == "adversarial" else None,
            "long_task_completed": final_success if spec.long_task else None,
            "observed_faults": faults,
        },
        "process_quality": {
            "tool_selection_accuracy": (
                selection_correct / selection_total if annotated and complete else None
            ),
            "selection_correct": selection_correct if annotated and complete else None,
            "selection_total": selection_total if annotated and complete else None,
            "invalid_step_ratio": invalid / proposals if finished_all and proposals else None,
            "invalid_step_basis": "errors_skips_and_annotated_misselections",
            "repeated_action_ratio": components["repeated_argument_call_ratio"]
            if complete
            else None,
            "tool_success_rate": components["tool_success_rate"],
            "trace_complete": complete,
        },
    }


def failure_attribution(row: dict, events=()) -> dict | None:
    """Failure stage evidence is not a claim of semantic root cause."""
    if row.get("success"):
        return None
    status = row.get("status", "unknown")
    category = "execution"
    if status in {"validation_error", "validation_infrastructure_error", "evaluation_unavailable"}:
        category = "evaluator"
    elif status in {"timeout"}:
        category = "timeout"
    elif status in {"round_limit", "context_budget", "context_protocol"}:
        category = "planning"
    else:
        errors = [e for e in events if e.get("error_category")]
        if errors:
            category = errors[-1]["error_category"]
    assessment = row.get("assessment", {})
    failed_rules = [r["id"] for r in assessment.get("rules", []) if r["passed"] is False]
    if status == "evaluation_rule_failed":
        failed = [r for r in assessment["rules"] if r["hard"] and r["passed"] is False]
        category = "planning" if failed and failed[0]["dimension"] == "process" else "execution"
    return {
        "category": category,
        "status": status,
        "failed_rules": failed_rules,
        "root_cause_confirmed": False,
    }


def attach_assessment(row, spec, events, answer):
    assessment = assess_case(row, spec, events, answer)
    row["assessment"] = assessment
    if row["success"] and not assessment["hard_rules_passed"]:
        row["success"] = False
        missing = any(r["hard"] and r["passed"] is None for r in assessment["rules"])
        row["status"] = "evaluation_unavailable" if missing else "evaluation_rule_failed"
    row["failure_attribution"] = failure_attribution(row, events)
    return row


def measurement(rows, section, metric):
    by_task = defaultdict(list)
    observed = 0
    for row in rows:
        value = row.get("assessment", {}).get(section, {}).get(metric)
        if value is not None:
            by_task[row["task_id"]].append(value)
            observed += 1
    values = [statistics.mean(v) for v in by_task.values()]
    return {
        "value": statistics.mean(values) if values else None,
        "observed_attempts": observed,
        "total_attempts": len(rows),
        "coverage": observed / len(rows) if rows else 0,
    }


def task_statistics(rows):
    grouped = defaultdict(list)
    for row in rows:
        grouped[row["task_id"]].append(row["success"] is True)
    return {
        "tasks": len(grouped),
        "attempts": len(rows),
        "success_rate": statistics.mean(statistics.mean(v) for v in grouped.values())
        if grouped
        else None,
        "attempt_success_rate": sum(r["success"] is True for r in rows) / len(rows)
        if rows
        else None,
        "all_repeats_pass_rate": sum(all(v) for v in grouped.values()) / len(grouped)
        if grouped
        else None,
    }


def summarize_dimensions(rows: list[dict]) -> dict:
    result = {}
    for variant in sorted({r["variant"] for r in rows}):
        selected = [r for r in rows if r["variant"] == variant]
        dimensions = {
            section: {metric: measurement(selected, section, metric) for metric in metrics}
            for section, metrics in {
                "effectiveness": ["answer_accuracy", "user_satisfaction"],
                "efficiency": ["steps", "tool_calls", "reported_tokens", "latency_seconds"],
                "robustness": ["exception_recovered", "adversarial_passed", "long_task_completed"],
                "process_quality": [
                    "tool_selection_accuracy",
                    "invalid_step_ratio",
                    "repeated_action_ratio",
                ],
            }.items()
        }
        dimensions["effectiveness"].update(task_statistics(selected))
        dimensions["efficiency"]["usage_complete"] = all(
            r.get("metrics", {}).get("usage_complete", False) for r in selected
        )
        strata = {}
        for field in ("scenario", "task_type", "split"):
            groups = defaultdict(list)
            for row in selected:
                value = row.get("assessment", {}).get("spec", {}).get(field, "unlabeled")
                groups[value].append(row)
            strata[field] = {name: task_statistics(group) for name, group in sorted(groups.items())}
        counts = Counter(
            r.get("assessment", {}).get("spec", {}).get("scenario", "unlabeled")
            for r in {r["task_id"]: r for r in selected}.values()
        )
        result[variant] = {
            "scorer_version": SCORER_VERSION,
            "dimensions": dimensions,
            "strata": strata,
            "dataset_mix": {
                name: {"tasks": counts[name], "target": fraction}
                for name, fraction in SCENARIOS.items()
            },
            "failures": dict(
                Counter(
                    (r.get("failure_attribution") or failure_attribution(r))["category"]
                    for r in selected
                    if not r["success"]
                )
            ),
            "optimization_candidates": optimization_candidates(selected),
        }
    return result


def optimization_candidates(rows):
    signals = []
    for row in rows:
        process = row.get("assessment", {}).get("process_quality", {})
        if not row["success"] or any(
            process.get(name) is not None and process[name] > 0
            for name in ("invalid_step_ratio", "repeated_action_ratio")
        ):
            signals.append(
                {
                    "task_id": row["task_id"],
                    "attempt": row["attempt"],
                    "success": row["success"],
                    "status": row["status"],
                    "process_quality": process,
                    "next_action": (
                        "检查失败规则和 Trace，验证归因；修改后用相同任务、模型和预算配对复测。"
                    ),
                }
            )
    return signals
