"""Quality evaluation commands: offline components, bounded judging, regression, calibration."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
import uuid
from pathlib import Path

from loguru import logger

from xclaw.ai.provider import OpenAIProvider
from xclaw.evaluation.evidence import build_case_packet, read_json
from xclaw.evaluation.judge import DIMENSIONS, RUBRIC_HASH, evaluate, grade_summary, parse_grade
from xclaw.evaluation.quality_report import average, calibrate, compare_records, quality_summary
from xclaw.evaluation.tasks import load_tasks
from xclaw.persistence import atomic_json
from xclaw.server.config import Config

PROVIDER_ERRORS = {"provider_balance_unavailable", "provider_configuration_error"}


def select_rows(rows, maximum):
    """Deterministic round-robin by category and outcome; avoid selecting only successes."""
    groups = {}
    for row in rows:
        groups.setdefault((row["category"], row["success"]), []).append(row)
    for group in groups.values():
        group.sort(key=lambda r: (r["task_id"], r["variant"], r["attempt"]))
    selected = []
    while len(selected) < maximum:
        found = False
        for key in sorted(groups):
            if groups[key] and len(selected) < maximum:
                selected.append(groups[key].pop(0))
                found = True
        if not found:
            break
    return selected


def records_report(output: Path, records, manifest):
    atomic_json(output / "records.json", records)
    summary = quality_summary(records)
    atomic_json(output / "summary.json", summary)
    lines = [
        "# XClaw 四维质量评审",
        "",
        "客观验收与模型评分分别保存；模型分数不会改变原任务成功/失败结果。",
        f"仅评审选定样本：{len(manifest['selected_cases'])} 个案例，"
        f"每例 {manifest['repeats']} 次。",
        "未评分/无法判断的维度留空，不能视为满分或零分。",
        "方案均分只描述已选样本；样本未配对时不能用于判断方案优劣。",
        "",
        "| 方案 | 已评分/选定 | 相关性 | 准确性 | 完整性 | 有用性 |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for variant, stats in summary.items():
        means = [
            f"{stats['dimension_means'][d]:.2f}"
            if stats["dimension_means"][d] is not None
            else "不可测"
            for d in DIMENSIONS
        ]
        lines.append(
            f"| {variant} | {stats['graded']}/{stats['selected']} | " + " | ".join(means) + " |"
        )
    lines += [
        "",
        "## 逐例结果",
        "",
        "| 案例 | 客观验收 | 评分状态 | 相关性 | 准确性 | 完整性 | 有用性 |",
        "|---|---|---|---:|---:|---:|---:|",
    ]
    for record in records:
        scores = [str(record.get("scores", {}).get(d) or "未评分") for d in DIMENSIONS]
        outcome = record["objective"].get("success")
        outcome_label = "通过" if outcome is True else "失败" if outcome is False else "不适用"
        lines.append(
            f"| {record['metadata']['case_id']} | {outcome_label} | {record['status']} | "
            + " | ".join(scores)
            + " |"
        )
    lines += ["", "## 逐例改进建议", ""]
    for record in records:
        recommendations = record.get("grade", {}).get("recommendations", [])
        if recommendations:
            lines.append(f"### {record['metadata']['case_id']}")
            lines.append("")
            lines.extend("- " + recommendation for recommendation in recommendations)
            lines.append("")
    lines += [
        f"本次新增模型请求：{sum(r.get('metrics', {}).get('model_calls', 0) for r in records)}；"
        f"已报告 Token：{sum(r.get('metrics', {}).get('reported_tokens', 0) for r in records)}。",
        "评分理由和逐字证据引用保存在 records.json。",
    ]
    lines += ["", "## 人工复核", ""]
    for record in records:
        if record.get("human_review_flags"):
            lines.append(
                f"- {record['metadata']['case_id']}: {', '.join(record['human_review_flags'])}"
            )
    lines += [
        "",
        "同模型评审存在自我偏好风险。变体标签已隐藏，但这不能消除全部偏差。",
        "导出人工标注后才能计算评审一致性；未标注时不报告评分准确率。",
    ]
    (output / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def load_experiment(args):
    root = Path(args.experiment).resolve()
    rows = read_json(root / "results.json", root)
    tasks = (
        {task.id: task for task in load_tasks(args.suite)} if getattr(args, "suite", None) else None
    )
    return root, [row for row in rows if row["status"] not in PROVIDER_ERRORS], tasks


def analyze(args):
    root, rows, tasks = load_experiment(args)
    output = Path(args.output or root / "quality-components").resolve()
    records = []
    for row in rows:
        try:
            packet = build_case_packet(row, root, tasks)
            records.append(
                {
                    "metadata": packet.metadata,
                    "objective": packet.objective,
                    "components": packet.components,
                    "status": "analyzed",
                }
            )
        except (OSError, ValueError, KeyError) as exc:
            records.append(
                {
                    "metadata": {
                        "case_id": f"{row['task_id']}-{row['variant']}-{row['attempt']}",
                        "variant": row["variant"],
                    },
                    "status": "evidence_error",
                    "error_type": type(exc).__name__,
                }
            )
    atomic_json(output / "records.json", records)
    summary = {}
    for variant in sorted({r["metadata"]["variant"] for r in records}):
        group = [r for r in records if r["metadata"]["variant"] == variant]
        known = [r["components"] for r in group if r["status"] == "analyzed"]
        metrics = {
            name: {
                "mean": average(k[name] for k in known),
                "measured_cases": sum(k[name] is not None for k in known),
            }
            for name in (
                "tool_success_rate",
                "expected_tool_group_coverage",
                "repeated_argument_call_ratio",
                "estimated_context_reduction_ratio",
                "dependency_plan_fallback_rate",
            )
        }
        summary[variant] = {
            "cases": len(group),
            "analyzed": len(known),
            "metrics": metrics,
            "protocol_errors": sum(k["protocol_errors"] for k in known),
            "context_budget_violations": sum(k["context_budget_violations"] for k in known),
        }
    atomic_json(output / "summary.json", summary)
    lines = [
        "# XClaw 组件评测",
        "",
        "本命令不调用模型。None 表示缺少记录或不适用。",
        "旧 trace 未保存参数指纹，重复参数调用比例不可测；关键事实的语义保留需单独评审。",
        "",
    ]
    for variant, group in summary.items():
        lines += [f"## {variant}", "", f"已分析 {group['analyzed']}/{group['cases']} 个案例。", ""]
        for metric, value in group["metrics"].items():
            mean = f"{value['mean']:.4f}" if value["mean"] is not None else "不可测"
            lines.append(f"- {metric}: {mean}；覆盖 {value['measured_cases']}/{group['cases']}")
    (output / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(output / "report.md")


async def judge(args):
    root, rows, tasks = load_experiment(args)
    if args.case_ids:
        ids = set(args.case_ids)
        rows = [r for r in rows if f"{r['task_id']}-{r['variant']}-{r['attempt']}" in ids]
        if len(rows) != len(ids):
            raise ValueError("Unknown or duplicate requested case IDs")
        if len(rows) > args.max_cases:
            raise ValueError("Requested cases exceed max-cases budget")
    else:
        rows = select_rows(rows, args.max_cases)
    if not rows:
        raise ValueError("No evaluable cases selected")
    config = Config.from_yaml(args.config)
    name = args.judge_provider or config.default_provider
    pc = next((p for p in config.providers if p.name == name), None)
    if pc is None or not pc.api_key:
        raise ValueError("Judge provider not configured")
    identity = name + ":" + hashlib.sha256(pc.base_url.encode()).hexdigest()[:16]
    output = Path(args.output or Path(".xclaw/quality") / uuid.uuid4().hex).resolve()
    cache = Path(args.cache).resolve()
    manifest = {
        "kind": "llm_judge",
        "source_experiment": str(root),
        "rubric_hash": RUBRIC_HASH,
        "judge_model": pc.model,
        "judge_identity": identity,
        "repeats": args.repeats,
        "selected_cases": [f"{r['task_id']}-{r['variant']}-{r['attempt']}" for r in rows],
        "source_cases": len(read_json(root / "results.json", root)),
        "max_attempts_per_judgment": 2,
        "max_output_tokens": args.max_tokens,
        "run_status": "running",
    }
    atomic_json(output / "manifest.json", manifest)
    provider = OpenAIProvider(
        pc.api_key, pc.base_url, auxiliary_thinking=False, judge_json_mode=not args.no_json_mode
    )
    records = []
    try:
        for row in rows:
            packet = build_case_packet(row, root, tasks)
            for repeat in range(1, args.repeats + 1):
                key = hashlib.sha256(
                    json.dumps(
                        {
                            "evidence": packet.fingerprint,
                            "rubric": RUBRIC_HASH,
                            "identity": identity,
                            "model": pc.model,
                            "repeat": repeat,
                            "max_tokens": args.max_tokens,
                            "json_mode": not args.no_json_mode,
                        },
                        sort_keys=True,
                    ).encode()
                ).hexdigest()
                cached_path = cache / f"{key}.json"
                cached = False
                if cached_path.exists() and not args.refresh:
                    record = read_json(cached_path, cache)
                    parsed = parse_grade(json.dumps(record["grade"], ensure_ascii=False), packet)
                    record.update(
                        **grade_summary(parsed, packet),
                        metadata=packet.metadata,
                        metrics={"model_calls": 0, "reported_tokens": 0, "usage_complete": True},
                    )
                    cached = True
                else:
                    record = await evaluate(
                        packet,
                        provider,
                        model=pc.model,
                        trace_dir=str(output / "traces"),
                        max_tokens=args.max_tokens,
                        input_budget=min(
                            args.input_budget, pc.context_window - args.max_tokens - 512
                        ),
                    )
                record.update(
                    cached=cached,
                    repeat=repeat,
                    judge_identity=identity,
                    requirements_hash=hashlib.sha256(
                        packet.sources["requirements"].encode()
                    ).hexdigest(),
                )
                record["same_model_as_generator"] = (
                    packet.metadata.get("generator_model") == pc.model
                )
                if record["status"] == "graded" and not cached:
                    atomic_json(cached_path, record)
                records.append(record)
                records_report(output, records, manifest)
                print(
                    f"{packet.metadata['case_id']} repeat={repeat}: {record['status']}", flush=True
                )
                if record["status"] == "provider_error":
                    manifest["run_status"] = "blocked_provider"
                    return
        manifest["run_status"] = "completed"
    except BaseException:
        manifest["run_status"] = "interrupted"
        raise
    finally:
        await provider._client.close()
        manifest["finished_judgments"] = len(records)
        atomic_json(output / "manifest.json", manifest)
    print(output / "report.md")


def main():
    parser = argparse.ArgumentParser(description="XClaw 多维质量评测")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("analyze", "judge"):
        p = sub.add_parser(name)
        p.add_argument("experiment")
        p.add_argument("--suite")
        p.add_argument("--output")
        if name == "judge":
            p.add_argument("--config", default="config.yaml")
            p.add_argument("--judge-provider")
            p.add_argument("--max-cases", type=int, default=3)
            p.add_argument("--case-ids", nargs="+")
            p.add_argument("--repeats", type=int, default=1)
            p.add_argument("--max-tokens", type=int, default=2048)
            p.add_argument("--input-budget", type=int, default=10000)
            p.add_argument("--cache", default=".xclaw/quality/cache")
            p.add_argument("--refresh", action="store_true")
            p.add_argument("--no-json-mode", action="store_true")
    p = sub.add_parser("compare")
    p.add_argument("baseline")
    p.add_argument("candidate")
    p.add_argument("--baseline-variant", required=True)
    p.add_argument("--candidate-variant", required=True)
    p.add_argument("--output", default=".xclaw/quality/regression.json")
    for name in ("labels", "calibrate"):
        p = sub.add_parser(name)
        p.add_argument("judgments")
        p.add_argument("--repeat", type=int, default=1)
        p.add_argument("--output", required=True)
        if name == "calibrate":
            p.add_argument("human_labels")
    args = parser.parse_args()
    logger.remove()
    logger.add(sys.stderr, level="WARNING")
    if args.command == "analyze":
        analyze(args)
    elif args.command == "judge":
        if (
            not 1 <= args.max_cases <= 1000
            or not 1 <= args.repeats <= 5
            or args.max_tokens < 1
            or args.input_budget < 1
        ):
            parser.error("Invalid explicit case, repeat or token budget")
        asyncio.run(judge(args))
    elif args.command == "compare":
        a, b = Path(args.baseline).resolve(), Path(args.candidate).resolve()
        old = [
            r
            for r in read_json(a / "records.json", a)
            if r["metadata"]["variant"] == args.baseline_variant
        ]
        new = [
            r
            for r in read_json(b / "records.json", b)
            if r["metadata"]["variant"] == args.candidate_variant
        ]
        atomic_json(args.output, compare_records(old, new))
    else:
        root = Path(args.judgments).resolve()
        records = [
            r for r in read_json(root / "records.json", root) if r.get("repeat", 1) == args.repeat
        ]
        if args.command == "labels":
            atomic_json(
                args.output,
                {
                    "rubric_hash": RUBRIC_HASH,
                    "labels": [
                        {
                            "case_id": r["metadata"]["case_id"],
                            "scores": {d: None for d in DIMENSIONS},
                        }
                        for r in records
                        if r["status"] == "graded"
                    ],
                },
            )
        else:
            labels = json.loads(Path(args.human_labels).read_text(encoding="utf-8"))
            if labels["rubric_hash"] != RUBRIC_HASH:
                raise ValueError("Human annotation rubric differs")
            atomic_json(args.output, calibrate(records, labels["labels"]))
        print(Path(args.output).resolve())


if __name__ == "__main__":
    main()
