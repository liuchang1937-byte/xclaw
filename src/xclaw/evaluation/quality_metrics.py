"""Component metrics with explicit missing-data coverage. No model calls."""

from __future__ import annotations

from collections import Counter


def trajectory_metrics(events: list[dict], expected_tool_groups=()) -> dict:
    finished = [e for e in events if e["event"] == "tool.finished"]
    started = [e for e in events if e["event"] == "tool.started"]
    successful = {e["tool"] for e in finished if e["status"] == "ok"}
    fingerprints = [e["argument_fingerprint"] for e in started if e.get("argument_fingerprint")]
    contexts = [e for e in events if e["event"] == "context.prepared"]
    plans = [e for e in events if e["event"] == "scheduling.plan" and len(e.get("steps", [])) > 1]
    enabled_plans = [e for e in plans if e.get("fallback_reason") != "disabled"]
    before = sum(e["before_estimated_tokens"] for e in contexts)
    after = sum(e["after_estimated_tokens"] for e in contexts)
    calls = [e for e in events if e["event"] == "model.finished"]
    main = [e for e in calls if e.get("purpose") == "main"]
    return {
        "tool_calls": len(finished),
        "tool_success_rate": sum(e["status"] == "ok" for e in finished) / len(finished)
        if finished
        else None,
        "tool_errors_by_name": dict(Counter(e["tool"] for e in finished if e["status"] != "ok")),
        "expected_tool_group_coverage": (
            sum(bool(set(group) & successful) for group in expected_tool_groups)
            / len(expected_tool_groups)
            if expected_tool_groups
            else None
        ),
        "repeated_argument_call_ratio": (
            (len(fingerprints) - len(set(fingerprints))) / len(fingerprints)
            if fingerprints and len(fingerprints) == len(started)
            else None
        ),
        "argument_fingerprint_coverage": len(fingerprints) / len(started) if started else None,
        "dependency_plan_fallback_rate": (
            sum(e.get("fallback_reason") is not None for e in enabled_plans) / len(enabled_plans)
            if enabled_plans
            else None
        ),
        "dependency_failure_skips": sum(e["event"] == "tool.skipped" for e in events),
        "context_preparations": len(contexts),
        "estimated_context_reduction_ratio": 1 - after / before if before else None,
        "context_budget_violations": sum(
            e["after_estimated_tokens"] > e["budget"] for e in contexts
        ),
        "context_blocked": sum(e["event"] == "context.blocked" for e in events),
        "context_summary_retries": sum(e["event"] == "context.summary_retry" for e in events),
        "protocol_errors": sum(
            e["event"] == "run.error"
            and e.get("error_type") in {"ContextProtocolError", "InvalidToolCallResponseError"}
            for e in events
        ),
        "last_main_output_truncated": main[-1].get("finish_reason") == "length" if main else None,
        "calls_by_purpose": dict(Counter(e.get("purpose", "unknown") for e in calls)),
    }
