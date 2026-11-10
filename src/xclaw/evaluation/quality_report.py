"""Coverage-aware summaries, matched regressions and human calibration."""

from __future__ import annotations

import math
import statistics
from collections import Counter

from xclaw.evaluation.judge import DIMENSIONS


def average(values):
    values = [v for v in values if v is not None]
    return statistics.mean(values) if values else None


def quality_summary(records: list[dict]) -> dict:
    result = {}
    for variant in sorted({r["metadata"].get("variant", "unknown") for r in records}):
        group = [r for r in records if r["metadata"].get("variant", "unknown") == variant]
        graded = [r for r in group if r["status"] == "graded"]
        result[variant] = {
            "selected": len(group),
            "graded": len(graded),
            "judge_coverage": len(graded) / len(group),
            "dimension_means": {d: average(r["scores"][d] for r in graded) for d in DIMENSIONS},
            "dimension_counts": {
                d: sum(r["scores"][d] is not None for r in graded) for d in DIMENSIONS
            },
            "requirement_satisfaction_ratio": average(
                r["requirement_satisfaction_ratio"] for r in graded
            ),
            "review_flags": dict(Counter(flag for r in graded for flag in r["human_review_flags"])),
            "ungraded": dict(Counter(r["status"] for r in group if r["status"] != "graded")),
            "judge_reported_tokens": sum(
                r.get("metrics", {}).get("reported_tokens", 0) for r in group
            ),
        }
    return result


def compare_records(baseline, candidate, *, quality_drop=0.5) -> dict:
    if not math.isfinite(quality_drop) or quality_drop <= 0:
        raise ValueError("Quality regression threshold must be positive")

    def key(row):
        meta = row["metadata"]
        return meta["task_id"], meta["attempt"]

    old, new = {key(r): r for r in baseline}, {key(r): r for r in candidate}
    if len(old) != len(baseline) or len(new) != len(candidate):
        raise ValueError("Select one variant per side; duplicate task/attempt identities")
    shared = sorted(old.keys() & new.keys())
    objective_regressions, quality_regressions, incomparable = [], [], []
    deltas = {d: [] for d in DIMENSIONS}
    for k in shared:
        a, b = old[k], new[k]
        if (
            not a["metadata"].get("task_hash")
            or a["metadata"]["task_hash"] != b["metadata"].get("task_hash")
            or a["metadata"].get("evaluation_hash") != b["metadata"].get("evaluation_hash")
        ):
            incomparable.append(
                {"task_id": k[0], "attempt": k[1], "reason": "task_baseline_changed"}
            )
            continue
        if a["objective"].get("success") is True and b["objective"].get("success") is False:
            objective_regressions.append({"task_id": k[0], "attempt": k[1]})
        if a["status"] != "graded" or b["status"] != "graded":
            continue
        if any(
            not a.get(name) or a.get(name) != b.get(name)
            for name in ("rubric_hash", "judge_model", "judge_identity", "requirements_hash")
        ):
            incomparable.append({"task_id": k[0], "attempt": k[1]})
            continue
        drops = []
        for d in DIMENSIONS:
            if a["scores"][d] is not None and b["scores"][d] is not None:
                delta = b["scores"][d] - a["scores"][d]
                deltas[d].append(delta)
                if delta <= -quality_drop:
                    drops.append(d)
        if drops:
            quality_regressions.append({"task_id": k[0], "attempt": k[1], "dimensions": drops})
    return {
        "matched_attempts": len(shared),
        "baseline_only": len(old.keys() - new.keys()),
        "candidate_only": len(new.keys() - old.keys()),
        "dimension_mean_deltas": {d: average(v) for d, v in deltas.items()},
        "dimension_compared_counts": {d: len(v) for d, v in deltas.items()},
        "objective_regressions": objective_regressions,
        "quality_regressions": quality_regressions,
        "incomparable_judgments": incomparable,
    }


def calibrate(records, labels) -> dict:
    by_id = {r["metadata"]["case_id"]: r for r in records if r["status"] == "graded"}
    if len(by_id) != sum(r["status"] == "graded" for r in records):
        raise ValueError("Calibration needs one judgment per case; select one repeat")
    results = {}
    for dimension in DIMENSIONS:
        pairs = []
        seen = set()
        for label in labels:
            case_id = label["case_id"]
            if case_id in seen:
                raise ValueError("Duplicate human label")
            seen.add(case_id)
            human = label["scores"].get(dimension)
            if human is None:
                continue
            if type(human) is not int or not 1 <= human <= 5:
                raise ValueError("Human scores must be integers from 1 to 5 or null")
            record = by_id.get(case_id)
            if record is None:
                continue
            score = record["scores"][dimension]
            if score is not None:
                pairs.append((human, score))
        n = len(pairs)
        if not n:
            results[dimension] = {
                "pairs": 0,
                "agreement": None,
                "within_one_point": None,
                "mae": None,
                "quadratic_kappa": None,
            }
            continue
        human_counts, judge_counts = Counter(a for a, _ in pairs), Counter(b for _, b in pairs)
        observed = sum((a - b) ** 2 for a, b in pairs) / n
        expected = (
            sum(
                (a - b) ** 2 * human_counts[a] * judge_counts[b]
                for a in range(1, 6)
                for b in range(1, 6)
            )
            / n**2
        )
        results[dimension] = {
            "pairs": n,
            "agreement": sum(a == b for a, b in pairs) / n,
            "within_one_point": sum(abs(a - b) <= 1 for a, b in pairs) / n,
            "mae": sum(abs(a - b) for a, b in pairs) / n,
            "quadratic_kappa": 1 - observed / expected if n >= 2 and expected > 0 else None,
        }
    return results
