"""Report serial versus scheduled live Agent attempts from an explicit experiment."""

import argparse
import json
import math
import statistics
from collections import Counter
from pathlib import Path

from xclaw.persistence import atomic_json


def analyze(root):
    rows = json.loads((root / "results.json").read_text(encoding="utf-8"))
    experiment = json.loads((root / "experiment.json").read_text(encoding="utf-8"))
    details = []
    for row in rows:
        case = root / f"{row['task_id']}-{row['variant']}-{row['attempt']}"
        events = [
            json.loads(line)
            for path in (case / "traces").glob("*.jsonl")
            for line in path.read_text(encoding="utf-8").splitlines()
        ]
        active = peak = 0
        for event in events:
            if event["event"] == "tool.started":
                active += 1
                peak = max(peak, active)
            elif event["event"] == "tool.finished":
                active -= 1
        plans = [
            e for e in events if e["event"] == "scheduling.plan" and len(e.get("steps", [])) > 1
        ]
        model = [e for e in events if e["event"] == "model.finished"]
        main = [e for e in model if e.get("purpose") == "main"]
        detail = {
            "task_id": row["task_id"],
            "variant": row["variant"],
            "attempt": row["attempt"],
            "success": row["success"],
            "status": row["status"],
            "seconds": row["duration_seconds"],
            "metrics": row["metrics"],
            "main_calls": row["metrics"].get("calls_by_purpose", {}).get("main", 0),
            "peak_parallel": peak,
            "eligible_batches": len(plans),
            "plan_modes": dict(Counter(p["mode"] for p in plans)),
            "planning_seconds": sum(
                e["duration_seconds"] for e in model if e.get("purpose") == "scheduling"
            ),
            "planning_tokens": sum(
                (e.get("usage") or {}).get("total_tokens", 0)
                for e in model
                if e.get("purpose") == "scheduling"
            ),
            "planning_fallbacks": sum(
                p.get("fallback_reason") is not None
                for p in plans
                if p.get("fallback_reason") != "disabled"
            ),
            "last_finish": main[-1].get("finish_reason") if main else None,
            "repeated_ratio": row.get("assessment", {})
            .get("process_quality", {})
            .get("repeated_action_ratio"),
        }
        details.append(detail)
    variants = {}
    for variant in sorted({r["variant"] for r in details}):
        group = [r for r in details if r["variant"] == variant]
        durations = sorted(r["seconds"] for r in group)
        tools = sum(r["metrics"].get("tool_calls", 0) for r in group)
        errors = sum(r["metrics"].get("tool_errors", 0) for r in group)
        tokens = sum(r["metrics"].get("reported_tokens", 0) for r in group)
        repeats = [r["repeated_ratio"] for r in group if r["repeated_ratio"] is not None]
        variants[variant] = {
            "attempts": len(group),
            "successes": sum(r["success"] for r in group),
            "success_rate": statistics.mean(r["success"] for r in group),
            "mean_seconds": statistics.mean(durations),
            "median_seconds": statistics.median(durations),
            "p95_seconds": durations[math.ceil(0.95 * len(group)) - 1]
            if len(group) >= 20
            else None,
            "mean_tokens": tokens / len(group),
            "total_tokens": tokens,
            "usage_complete": all(r["metrics"].get("usage_complete") for r in group),
            "mean_main_calls": statistics.mean(r["main_calls"] for r in group),
            "mean_all_model_calls": statistics.mean(r["metrics"]["model_calls"] for r in group),
            "mean_tool_calls": tools / len(group),
            "tool_calls": tools,
            "tool_errors": errors,
            "tool_error_rate": errors / tools if tools else None,
            "tool_skipped": sum(r["metrics"].get("tool_skipped", 0) for r in group),
            "maximum_parallel": max(r["peak_parallel"] for r in group),
            "parallel_attempts": sum(r["peak_parallel"] > 1 for r in group),
            "eligible_batches": sum(r["eligible_batches"] for r in group),
            "planning_seconds": sum(r["planning_seconds"] for r in group),
            "planning_tokens": sum(r["planning_tokens"] for r in group),
            "planning_calls": sum(
                r["metrics"].get("calls_by_purpose", {}).get("scheduling", 0) for r in group
            ),
            "planning_fallbacks": sum(r["planning_fallbacks"] for r in group),
            "mean_repeated_argument_ratio": statistics.mean(repeats) if repeats else None,
            "failures": [
                {k: r[k] for k in ("task_id", "attempt", "status", "last_finish")}
                for r in group
                if not r["success"]
            ],
        }
    indexed = {(r["task_id"], r["attempt"], r["variant"]): r for r in details}
    pairs = []
    for task, attempt in sorted({(r["task_id"], r["attempt"]) for r in details}):
        a = indexed.get((task, attempt, "summary_serial"))
        b = indexed.get((task, attempt, "summary_scheduled"))
        if a and b:
            pairs.append(
                {
                    "task_id": task,
                    "attempt": attempt,
                    "serial_success": a["success"],
                    "dag_success": b["success"],
                    "serial_seconds": a["seconds"],
                    "dag_seconds": b["seconds"],
                }
            )
    successful = [r for r in pairs if r["serial_success"] and r["dag_success"]]
    paired = {
        "matched_pairs": len(pairs),
        "both_successful": len(successful),
        "serial_only_success": sum(r["serial_success"] and not r["dag_success"] for r in pairs),
        "dag_only_success": sum(r["dag_success"] and not r["serial_success"] for r in pairs),
        "successful_pair_net_speedup": sum(r["serial_seconds"] for r in successful)
        / sum(r["dag_seconds"] for r in successful)
        if successful
        else None,
        "successful_pair_mean_delta_seconds": statistics.mean(
            r["dag_seconds"] - r["serial_seconds"] for r in successful
        )
        if successful
        else None,
    }
    controls = {
        "same_settings": len({json.dumps(r["settings"], sort_keys=True) for r in rows}) == 1,
        "same_prompt": len({r["provenance"]["prompt_hash"] for r in rows}) == 1,
        "same_tools": len({r["provenance"]["tools_hash"] for r in rows}) == 1,
        "same_sources": len({json.dumps(r["provenance"]["sources"], sort_keys=True) for r in rows})
        == 1,
        "same_task_versions": all(
            len({r["task_hash"] for r in rows if r["task_id"] == task}) == 1
            for task in {r["task_id"] for r in rows}
        ),
    }
    output = {
        "run_status": experiment["run_status"],
        "complete": experiment["run_status"] == "completed"
        and len(rows) == experiment["planned_attempts"],
        "controls": controls,
        "variants": variants,
        "paired": paired,
        "pairs": pairs,
        "details": details,
    }
    atomic_json(root / "comparison.json", output)
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path)
    args = parser.parse_args()
    report = analyze(args.run.resolve())
    print(json.dumps({k: v for k, v in report.items() if k not in {"pairs", "details"}}, indent=2))
