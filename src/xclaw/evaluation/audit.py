"""Offline replay, feedback import and fail-closed paired regression gates."""

from __future__ import annotations

import argparse
import json
import math
import statistics
from pathlib import Path

from xclaw.evaluation.assessment import attach_assessment, summarize_dimensions, task_statistics
from xclaw.evaluation.evidence import build_case_packet, case_directory, read_events
from xclaw.evaluation.spec import SCORER_VERSION, EvaluationSpec, digest, evaluation_hash
from xclaw.persistence import atomic_json


def load_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _index(rows, variant=None):
    selected = [r for r in rows if variant is None or r["variant"] == variant]
    indexed = {
        (r["task_id"], r["attempt"], r["variant"] if variant is None else "selected"): r
        for r in selected
    }
    if len(indexed) != len(selected):
        raise ValueError("duplicate task/variant/attempt identities")
    return indexed


def check_gate(
    baseline: Path,
    candidate: Path,
    *,
    change=None,
    min_success=0.8,
    max_success_drop=0.0,
    max_cost_increase=None,
    max_latency_increase=None,
    baseline_variant=None,
    candidate_variant=None,
) -> dict:
    for value in (min_success, max_success_drop):
        if not math.isfinite(value) or not 0 <= value <= 1:
            raise ValueError("success thresholds must be finite rates in [0,1]")
    for value in (max_cost_increase, max_latency_increase):
        if value is not None and (not math.isfinite(value) or value < 0):
            raise ValueError("cost/latency thresholds must be nonnegative finite ratios")
    if bool(baseline_variant) != bool(candidate_variant):
        raise ValueError("select a variant on both sides")
    if change not in {None, "model", "prompt", "tools", "runtime", "scheduling"}:
        raise ValueError("unknown controlled change")
    before, after = (
        load_json(baseline / "experiment.json"),
        load_json(candidate / "experiment.json"),
    )
    old = _index(load_json(baseline / "results.json"), baseline_variant)
    new = _index(load_json(candidate / "results.json"), candidate_variant)
    blockers, regressions, incomparable, pairs = [], [], [], []
    for side, meta, path in (("baseline", before, baseline), ("candidate", after, candidate)):
        rows = load_json(path / "results.json")
        if meta.get("run_status") != "completed" or meta.get("selection") != "full":
            blockers.append(f"{side}: requires a completed full run")
        if meta.get("split") != "evaluation":
            blockers.append(f"{side}: holdout results cannot replace the product regression gate")
        planned = {
            (task, attempt, variant)
            for task in meta.get("tasks", [])
            for attempt in range(1, meta.get("repeats", 0) + 1)
            for variant in meta.get("variants", [])
        }
        actual = {(r["task_id"], r["attempt"], r["variant"]) for r in rows}
        if not planned or actual != planned or len(rows) != len(planned):
            blockers.append(f"{side}: missing, unexpected or duplicate attempts")
        if any((r.get("failure_attribution") or {}).get("category") == "evaluator" for r in rows):
            blockers.append(f"{side}: evaluator failed; repair evidence before comparing")
    for field in ("dataset_hash", "evaluation_hash", "suite", "scorer_version", "concurrency"):
        if before.get(field) is None or before.get(field) != after.get(field):
            blockers.append(f"incompatible {field}")
    shared = sorted(old.keys() & new.keys())
    if not shared or old.keys() != new.keys():
        blockers.append("task/attempt coverage changed; inspect shared and new tasks separately")
    for key in shared:
        a, b = old[key], new[key]
        reasons = []
        for field in ("task_hash", "evaluation_hash", "suite"):
            if not a.get(field) or a.get(field) != b.get(field):
                reasons.append(field)
        sa, sb = dict(a.get("settings", {})), dict(b.get("settings", {}))
        if change == "model":
            sa.pop("model", None)
            sb.pop("model", None)
        if not sa or sa != sb:
            reasons.append("settings/budget")
        pa, pb = a.get("provenance", {}), b.get("provenance", {})
        for field, allowed in (
            ("model", "model"),
            ("prompt_hash", "prompt"),
            ("tools_hash", "tools"),
            ("image", None),
            ("python", None),
            ("platform", None),
            ("dependencies", None),
        ):
            if not pa.get(field) or not pb.get(field):
                reasons.append("missing " + field)
            elif pa[field] != pb[field] and (allowed is None or change != allowed):
                reasons.append(field)
        # Source snapshots distinguish a changed prompt/schema from changed implementations.
        for field, allowed in (
            ("runtime", "runtime"),
            ("tools", "tools"),
            ("prompts", "prompt"),
            ("scorer", None),
        ):
            va, vb = pa.get("sources", {}).get(field), pb.get("sources", {}).get(field)
            if not va or not vb or (va != vb and (allowed is None or change != allowed)):
                reasons.append("source:" + field)
        if reasons:
            incomparable.append({"task_id": key[0], "attempt": key[1], "reasons": reasons})
            continue
        pairs.append((a, b))
        if a["success"] and not b["success"]:
            regressions.append({"task_id": key[0], "attempt": key[1], "variant": b["variant"]})
    if incomparable:
        blockers.append("incomparable attempts")
    # Evaluate each selected variant separately: strong scenarios cannot hide critical failures.
    variant_results = {}
    for variant in sorted({r["variant"] for r in new.values()}):
        selected = [b for a, b in pairs if b["variant"] == variant]
        previous = [a for a, b in pairs if b["variant"] == variant]
        if not selected:
            continue
        current_stats, old_stats = task_statistics(selected), task_statistics(previous)
        current_rate, old_rate = current_stats["success_rate"], old_stats["success_rate"]
        if current_rate < min_success:
            blockers.append(f"{variant}: below minimum success rate")
        if old_rate - current_rate > max_success_drop + 1e-12:
            blockers.append(f"{variant}: task success rate regressed")
        for row in selected:
            if row.get("assessment", {}).get("spec", {}).get("critical") and not row["success"]:
                blockers.append(f"{variant}: critical task failed: {row['task_id']}")
        deltas = {}
        for name, metric, limit in (
            ("tokens", "reported_tokens", max_cost_increase),
            ("latency", "latency_seconds", max_latency_increase),
            ("steps", "steps", None),
        ):
            av = [r.get("assessment", {}).get("efficiency", {}).get(metric) for r in previous]
            bv = [r.get("assessment", {}).get("efficiency", {}).get(metric) for r in selected]
            known = all(v is not None for v in av + bv)
            if name == "tokens":
                known = known and all(
                    r.get("metrics", {}).get("usage_complete") for r in previous + selected
                )
            ratio = None
            delta = None
            if known:
                base_mean, candidate_mean = statistics.mean(av), statistics.mean(bv)
                delta = candidate_mean - base_mean
                ratio = (
                    candidate_mean / base_mean - 1
                    if base_mean
                    else (0 if not candidate_mean else None)
                )
            if limit is not None:
                if not known:
                    blockers.append(f"{variant}: {name} coverage incomplete")
                elif (ratio is None and delta > 0) or (ratio is not None and ratio > limit + 1e-12):
                    blockers.append(f"{variant}: {name} increase exceeds budget")
            deltas[name] = {"delta": delta, "relative_change": ratio, "complete": known}
        variant_results[variant] = {
            "baseline": old_stats,
            "candidate": current_stats,
            "success_delta": current_rate - old_rate,
            "efficiency": deltas,
            "candidate_strata": summarize_dimensions(selected)[variant]["strata"],
        }
    if regressions:
        blockers.append("previously passing task attempts regressed")
    return {
        "passed": not blockers,
        "controlled_change": change,
        "blockers": sorted(set(blockers)),
        "matched_attempts": len(pairs),
        "baseline_only": len(old.keys() - new.keys()),
        "candidate_only": len(new.keys() - old.keys()),
        "incomparable": incomparable,
        "regressions": regressions,
        "variants": variant_results,
        "policy": {
            "min_success": min_success,
            "max_success_drop": max_success_drop,
            "max_cost_increase": max_cost_increase,
            "max_latency_increase": max_latency_increase,
        },
    }


def replay(root: Path) -> dict:
    """Recompute only deterministic rules from saved evidence; no model or code execution."""
    from xclaw.evaluation.runner import source_versions

    scorer_source = source_versions()["scorer"]
    rows = load_json(root / "results.json")
    replayed = []
    for original in rows:
        row = dict(original)
        case = case_directory(row, root)
        task = load_json(case / "task.json")
        spec = EvaluationSpec.model_validate(task.get("evaluation", {}))
        if task["task_hash"] != row["task_hash"]:
            raise ValueError("task evidence baseline mismatch")
        if row.get("evaluation_hash") != evaluation_hash(spec):
            raise ValueError("scoring contract mismatch; use the original scorer and dataset")
        previous = row.pop("assessment", {})
        if previous.get("scorer_version") != SCORER_VERSION:
            raise ValueError("use the original scorer version to replay this experiment")
        if original.get("provenance", {}).get("sources", {}).get("scorer") != scorer_source:
            raise ValueError("scorer source changed; restore the original scorer before replay")
        row["success"] = previous.get("objective_success", False)
        # Restore the runtime acceptance status before applying the saved rules again.
        if row["status"] in {"evaluation_rule_failed", "evaluation_unavailable"}:
            row["status"] = "completed"
        answer_path = case / "response.txt"
        if answer_path.is_symlink():
            raise ValueError("response evidence must remain inside case directory")
        answer = answer_path.read_text(encoding="utf-8") if answer_path.exists() else None
        attach_assessment(row, spec, read_events(case), answer)
        replayed.append(row)
    report = {
        "kind": "deterministic_replay",
        "model_called": False,
        "objective_reexecuted": False,
        "dimensions": summarize_dimensions(replayed),
        "rows": replayed,
    }
    atomic_json(root / "replay.json", report)
    return report


def export_review_pairs(
    baseline: Path,
    candidate: Path,
    output: Path,
    *,
    baseline_variant: str,
    candidate_variant: str,
    seed="review-v1",
) -> dict:
    """Export successful matched trajectories with blinded, counterbalanced sides."""
    old = _index(load_json(baseline / "results.json"), baseline_variant)
    new = _index(load_json(candidate / "results.json"), candidate_variant)
    pairs, key = [], []
    for identity in sorted(old.keys() & new.keys()):
        a, b = old[identity], new[identity]
        if not a["success"] or not b["success"]:
            continue
        if any(not a.get(k) or a[k] != b.get(k) for k in ("task_hash", "evaluation_hash")):
            continue
        pair_id = digest([seed, identity])[:16]
        sides = [("baseline", a, baseline), ("candidate", b, candidate)]
        if int(pair_id, 16) % 2:
            sides.reverse()
        review = {"pair_id": pair_id}
        mapping = {"pair_id": pair_id}
        for side, (label, row, root) in zip(("A", "B"), sides):
            review[side] = {
                "evidence": build_case_packet(row, root).payload(),
                "objective_passed": row["success"],
                "efficiency": row.get("assessment", {}).get("efficiency", {}),
                "rule_checks": row.get("assessment", {}).get("rules", []),
            }
            mapping[side] = {
                "origin": label,
                "task_id": row["task_id"],
                "variant": row["variant"],
                "attempt": row["attempt"],
            }
        pairs.append(review)
        key.append(mapping)
    output.mkdir(parents=True, exist_ok=False)
    report = {"kind": "blind_paired_review", "pairs": pairs, "seed": seed}
    atomic_json(output / "review.json", report)
    atomic_json(output / "private_key.json", key)
    return report


def import_feedback(root: Path, labels: Path) -> dict:
    """Join trusted human/product observations; never consume agent self-scores."""
    rows = load_json(root / "results.json")
    indexed = _index(rows)
    seen = set()
    for label in load_json(labels):
        if set(label) != {"task_id", "variant", "attempt", "score", "source", "evidence"}:
            raise ValueError("feedback requires identity, score, source and evidence")
        if label["source"] not in {"human", "user", "product_signal"} or not label["evidence"]:
            raise ValueError("feedback needs independent source and evidence")
        if type(label["score"]) is not int or not 1 <= label["score"] <= 5:
            raise ValueError("satisfaction score must be an integer in [1,5]")
        key = label["task_id"], label["attempt"], label["variant"]
        if key not in indexed or key in seen:
            raise ValueError("unknown or duplicate feedback identity")
        seen.add(key)
        indexed[key]["assessment"]["effectiveness"]["user_satisfaction"] = label["score"]
    report = {
        "source_hash": digest(load_json(labels)),
        "labelled_attempts": len(seen),
        "dimensions": summarize_dimensions(rows),
    }
    atomic_json(root / "feedback.json", report)
    return report


def main():
    parser = argparse.ArgumentParser(description="离线四维评测、反馈导入与回归门禁")
    sub = parser.add_subparsers(dest="command", required=True)
    replay_parser = sub.add_parser("replay")
    replay_parser.add_argument("run", type=Path)
    feedback = sub.add_parser("feedback")
    feedback.add_argument("run", type=Path)
    feedback.add_argument("labels", type=Path)
    gate = sub.add_parser("gate")
    gate.add_argument("baseline", type=Path)
    gate.add_argument("candidate", type=Path)
    gate.add_argument("--change", choices=["model", "prompt", "tools", "runtime", "scheduling"])
    gate.add_argument("--min-success", type=float, default=0.8)
    gate.add_argument("--max-success-drop", type=float, default=0)
    gate.add_argument("--max-cost-increase", type=float)
    gate.add_argument("--max-latency-increase", type=float)
    gate.add_argument("--baseline-variant")
    gate.add_argument("--candidate-variant")
    review = sub.add_parser("review-pairs")
    review.add_argument("baseline", type=Path)
    review.add_argument("candidate", type=Path)
    review.add_argument("output", type=Path)
    review.add_argument("--baseline-variant", required=True)
    review.add_argument("--candidate-variant", required=True)
    args = parser.parse_args()
    if args.command == "replay":
        replay(args.run.resolve())
        print(args.run / "replay.json")
    elif args.command == "feedback":
        import_feedback(args.run.resolve(), args.labels)
        print(args.run / "feedback.json")
    elif args.command == "review-pairs":
        report = export_review_pairs(
            args.baseline.resolve(),
            args.candidate.resolve(),
            args.output.resolve(),
            baseline_variant=args.baseline_variant,
            candidate_variant=args.candidate_variant,
        )
        print(f"{len(report['pairs'])} pairs: {args.output / 'review.json'}")
    else:
        options = vars(args).copy()
        for field in ("command", "baseline", "candidate"):
            options.pop(field)
        report = check_gate(args.baseline, args.candidate, **options)
        atomic_json(args.candidate / "gate.json", report)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        if not report["passed"]:
            raise SystemExit(2)


if __name__ == "__main__":
    main()
