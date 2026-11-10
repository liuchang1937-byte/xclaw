"""Controlled same-call scheduler comparison, including live planning overhead.

Tool I/O is simulated with bounded asyncio waits and fixed data. This is not a
production throughput benchmark. The actual ToolScheduler and SandboxRouter run.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import platform
import statistics
import sys
import time
import uuid
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path

from loguru import logger

from xclaw.ai.provider import OpenAIProvider
from xclaw.ai.types import ChatMessage, ToolCall
from xclaw.coding import CodingAgent
from xclaw.core.execution import Capability
from xclaw.core.scheduling import SchedulingConfig, tool_failed
from xclaw.core.tool import Tool
from xclaw.evaluation.runner import dependency_versions, source_versions
from xclaw.evaluation.spec import digest
from xclaw.persistence import atomic_json
from xclaw.server.config import Config
from xclaw.telemetry import RunTrace, activate, emit


@dataclass
class BatchCase:
    id: str
    context: str
    calls: list[tuple[str, str]]  # (call ID/key, tool name), topological input order
    delay: float
    dependencies: dict[str, list[str]] = field(default_factory=dict)
    fail: list[str] = field(default_factory=list)
    blocked: list[str] = field(default_factory=list)


def cases(slow=2.0, fast=0.05):
    return [
        BatchCase("single_read", "只需读取这一项。", [("a", "fetch_item")], fast),
        BatchCase(
            "four_fast_reads",
            "四个独立数据项，可独立读取，无依赖。",
            [(str(i), "fetch_item") for i in range(4)],
            fast,
        ),
        BatchCase(
            "four_slow_reads",
            "四个独立远程数据项，可独立读取，无依赖。",
            [(str(i), "fetch_item") for i in range(4)],
            slow,
        ),
        BatchCase(
            "eight_slow_reads",
            "八个独立远程数据项，可独立读取，无依赖。",
            [(str(i), "fetch_item") for i in range(8)],
            slow,
        ),
        BatchCase(
            "fork_join",
            "用户档案与权限是独立查询；二者完成后才能检查访问资格。参数已知，无需延期。",
            [
                ("profile", "fetch_profile"),
                ("permissions", "fetch_permissions"),
                ("check", "check_entitlement"),
            ],
            slow / 2,
            {"check": ["profile", "permissions"]},
        ),
        BatchCase(
            "exclusive_write",
            "先读取旧缓存，再更新缓存，最后读取新缓存。写入有副作用，不能与读取重叠。",
            [("before", "read_before"), ("write", "write_cache"), ("after", "read_after")],
            slow / 4,
            {"write": ["before"], "after": ["write"]},
        ),
        BatchCase(
            "failed_prerequisite",
            "用户档案查询是资格检查的前置，前置失败时禁止执行资格检查；"
            "另一项公告查询独立，前置失败也要完成。参数已知，无需延期。",
            [
                ("profile", "fetch_profile"),
                ("notice", "fetch_item"),
                ("check", "check_entitlement"),
            ],
            fast,
            {"check": ["profile"]},
            ["profile"],
            ["check"],
        ),
    ]


DESCRIPTIONS = {
    "fetch_item": "读取 key 指定的独立数据项，不依赖其他调用，没有副作用。",
    "fetch_profile": "读取用户档案，调用成功后档案缓存可供 check_entitlement 使用。",
    "fetch_permissions": "读取用户权限，与用户档案查询独立；成功后缓存可供资格检查使用。",
    "check_entitlement": "读取前置查询的缓存检查资格，必须等待上下文要求的档案/权限查询成功；"
    "前置失败时禁止执行。不需要根据结果修改参数，不应 defer。",
    "read_before": "读取更新前的旧缓存，必须先于 write_cache；无副作用。",
    "write_cache": "更新缓存，有写入副作用，必须在 read_before 完成后、read_after 之前执行。",
    "read_after": "读取更新后的新缓存，必须等待 write_cache 成功；无副作用。",
}


async def run_batch(case, scheduled, attempt, root, provider, model, *, max_parallel=4):
    variant = "dag" if scheduled else "serial"
    directory = root / f"{case.id}-{variant}-{attempt}"
    workspace = directory / "workspace"
    workspace.mkdir(parents=True, exist_ok=False)
    bot = CodingAgent(
        provider,
        model=model,
        workspace_dir=str(workspace),
        register_default_tools=False,
        tool_scheduling=SchedulingConfig(enabled=scheduled, max_parallel=max_parallel),
    )
    trace = RunTrace(directory / "traces", capture_content=True)
    spans, active = {}, set()
    peak = 0
    violations = []

    def implementation(name):
        async def invoke(key: str):
            nonlocal peak
            writing = name == "write_cache"
            if writing and active or any(spans[k]["writing"] for k in active):
                violations.append({"kind": "write_overlap", "key": key})
            required = case.dependencies.get(key, [])
            for parent in required:
                if parent not in spans or spans[parent].get("ended") is None:
                    violations.append({"kind": "dependency_started_early", "key": key})
                elif not spans[parent]["ok"]:
                    violations.append({"kind": "failed_parent_executed", "key": key})
            spans[key] = {
                "started": time.perf_counter(),
                "ended": None,
                "writing": writing,
                "ok": False,
            }
            active.add(key)
            peak = max(peak, len(active))
            emit("probe.tool_started", key=key, tool=name, active=len(active))
            try:
                await asyncio.sleep(case.delay)
                if key in case.fail:
                    return "错误: fixed injected prerequisite failure"
                if key in case.blocked or any(v["key"] == key for v in violations):
                    return "错误: prerequisite contract violated"
                if writing:
                    (workspace / "cache.txt").write_text("fixed-value", encoding="utf-8")
                spans[key]["ok"] = True
                return json.dumps({"key": key, "value": "fixed-value"}, sort_keys=True)
            finally:
                spans[key]["ended"] = time.perf_counter()
                active.remove(key)
                emit("probe.tool_finished", key=key, tool=name, ok=spans[key]["ok"])

        return invoke

    for name in sorted({name for _, name in case.calls}):
        writing = name == "write_cache"
        bot.register_tool(
            Tool(
                name,
                DESCRIPTIONS[name],
                implementation(name),
                capabilities=frozenset(
                    {Capability.FILE_WRITE if writing else Capability.FILE_READ}
                ),
                parallel_safe=not writing,
            )
        )
    calls = [
        ToolCall(id=key, function={"name": name, "arguments": json.dumps({"key": key})})
        for key, name in case.calls
    ]
    started = time.perf_counter()
    results = []
    error_type = None
    with activate(trace):
        trace.emit("probe.started", case_id=case.id, variant=variant, attempt=attempt)
        try:
            results = await asyncio.wait_for(
                bot._act(calls, [ChatMessage(role="user", content=case.context)]),
                timeout=60,
            )
        except Exception as exc:
            error_type = type(exc).__name__
        elapsed = time.perf_counter() - started
        trace.emit("probe.finished", duration_seconds=elapsed, error_type=error_type)
    await bot.sandbox_router.stop()
    expected_keys = {key for key, _ in case.calls} - set(case.blocked)
    good_keys = expected_keys - set(case.fail)
    by_id = {r.tool_call_id: r for r in results}
    checks = {
        "expected_execution": set(spans) == expected_keys,
        "successful_results": all(
            k in by_id and not tool_failed(by_id[k].content) for k in good_keys
        ),
        "injected_failure_observed": all(k in spans and not spans[k]["ok"] for k in case.fail),
        "blocked_results": all(
            k in by_id and "工具未执行" in by_id[k].content for k in case.blocked
        ),
        "order_preserved": [r.tool_call_id for r in results] == [c.id for c in calls],
        "dependency_correct": not violations,
        "parallel_limit": peak <= max_parallel,
    }
    plan = next((e for e in trace.events if e["event"] == "scheduling.plan"), {})
    finished = [e for e in trace.events if e["event"] == "tool.finished"]
    model_calls = [e for e in trace.events if e["event"] == "model.finished"]
    execution = (
        (max(s["ended"] for s in spans.values()) - min(s["started"] for s in spans.values()))
        if spans
        else None
    )
    row = {
        "case_id": case.id,
        "variant": variant,
        "attempt": attempt,
        "success": error_type is None and all(checks.values()),
        "checks": checks,
        "duration_seconds": elapsed,
        "execution_seconds": execution,
        "sum_tool_seconds": sum(e["duration_seconds"] for e in finished),
        "planning_seconds": sum(e["duration_seconds"] for e in model_calls),
        "peak_parallel": peak,
        "tool_proposals": len(calls),
        "tool_executed": len(spans),
        "tool_errors": sum(e["status"] != "ok" for e in finished),
        "tool_skipped": sum(e["event"] == "tool.skipped" for e in trace.events),
        "model_steps": len(model_calls),
        "metrics": trace.metrics(),
        "plan": plan,
        "violations": violations,
        "error_type": error_type,
        "fatal_provider_error": any(
            e.get("http_status") in {400, 401, 402, 403, 404, 405, 410, 422} for e in model_calls
        ),
        "spans": spans,
        "fault_case": bool(case.fail),
    }
    atomic_json(directory / "result.json", row)
    return row


def summarize(rows):
    output = {}
    for variant in ("serial", "dag"):
        group = [r for r in rows if r["variant"] == variant]
        normal = [r for r in group if not r["fault_case"]]
        faults = [r for r in group if r["fault_case"]]
        durations = sorted(r["duration_seconds"] for r in group)
        output[variant] = {
            "attempts": len(group),
            "success_rate": statistics.mean(r["success"] for r in group),
            "normal_successes": sum(r["success"] for r in normal),
            "normal_attempts": len(normal),
            "failure_block_successes": sum(r["success"] for r in faults),
            "failure_cases": len(faults),
            "mean_seconds": statistics.mean(durations),
            "median_seconds": statistics.median(durations),
            "p95_seconds": durations[math.ceil(len(group) * 0.95) - 1]
            if len(group) >= 20
            else None,
            "mean_execution_seconds": statistics.mean(
                r["execution_seconds"] for r in group if r["execution_seconds"] is not None
            ),
            "mean_planning_seconds": statistics.mean(r["planning_seconds"] for r in group),
            "mean_tokens": statistics.mean(r["metrics"]["reported_tokens"] for r in group),
            "total_tokens": sum(r["metrics"]["reported_tokens"] for r in group),
            "usage_complete": all(r["metrics"]["usage_complete"] for r in group),
            "mean_model_steps": statistics.mean(r["model_steps"] for r in group),
            "maximum_parallel": max(r["peak_parallel"] for r in group),
            "tool_errors": sum(r["tool_errors"] for r in group),
            "tool_executed": sum(r["tool_executed"] for r in group),
            "tool_skipped": sum(r["tool_skipped"] for r in group),
            "dependency_violations": sum(len(r["violations"]) for r in group),
            "fallbacks": dict(Counter(str(r["plan"].get("fallback_reason")) for r in group)),
        }
    by_case = {}
    for name in sorted({r["case_id"] for r in rows}):
        pair = {}
        for variant in ("serial", "dag"):
            group = [r for r in rows if r["case_id"] == name and r["variant"] == variant]
            pair[variant] = {
                "attempts": len(group),
                "successes": sum(r["success"] for r in group),
                "seconds": statistics.mean(r["duration_seconds"] for r in group),
                "execution_seconds": statistics.mean(
                    r["execution_seconds"] for r in group if r["execution_seconds"] is not None
                ),
                "planning_seconds": statistics.mean(r["planning_seconds"] for r in group),
                "tokens": statistics.mean(r["metrics"]["reported_tokens"] for r in group),
                "peak_parallel": max(r["peak_parallel"] for r in group),
            }
        pair["net_speedup"] = pair["serial"]["seconds"] / pair["dag"]["seconds"]
        by_case[name] = pair
    return {"variants": output, "by_case": by_case}


def write_report(root, rows):
    summary = summarize(rows)
    atomic_json(root / "summary.json", summary)
    lines = [
        "# 同调用序列：依赖图调度与串行调度",
        "",
        "固定数据、工具参数与输入顺序；工具延迟为受控模拟 I/O，调度模型为真实 API。",
        "端到端批次时间包含规划请求；没有 Agent 主模型请求，不能当作完整业务延迟。",
        "故障传播单列，普通串行按现有实现继续调用后继，不人为补入依赖图。",
        "",
        "| 场景 | 串行成功 | DAG成功 | 串行总耗时(s) | DAG总耗时(s) | "
        "DAG执行(s) | DAG规划(s) | 净加速比 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for name, stats in summary["by_case"].items():
        a, b = stats["serial"], stats["dag"]
        lines.append(
            f"| {name} | {a['successes']}/{a['attempts']} | {b['successes']}/{b['attempts']} | "
            f"{a['seconds']:.3f} | {b['seconds']:.3f} | {b['execution_seconds']:.3f} | "
            f"{b['planning_seconds']:.3f} | {stats['net_speedup']:.2f}x |"
        )
    (root / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


async def run(args):
    config = Config.from_yaml(args.config)
    selected = next(p for p in config.providers if p.name == config.default_provider)
    if not selected.api_key:
        raise ValueError("provider key missing")
    root = Path(args.output).resolve() / uuid.uuid4().hex
    root.mkdir(parents=True)
    dataset = cases(args.slow_delay, args.fast_delay)
    manifest = {
        "kind": "controlled_io_live_planner",
        "model": selected.model,
        "provider_name": selected.name,
        "auxiliary_thinking": selected.auxiliary_thinking,
        "repeats": args.repeats,
        "concurrency": 1,
        "max_parallel": 4,
        "dataset": [asdict(c) for c in dataset],
        "dataset_hash": digest([asdict(c) for c in dataset]),
        "source_versions": source_versions(),
        "dependencies": dependency_versions(),
        "probe_hash": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "planned_attempts": len(dataset) * args.repeats * 2,
        "run_status": "running",
    }
    atomic_json(root / "experiment.json", manifest)
    print(root, flush=True)
    provider = OpenAIProvider(
        selected.api_key, selected.base_url, auxiliary_thinking=selected.auxiliary_thinking
    )
    rows = []
    try:
        for attempt in range(1, args.repeats + 1):
            for case in dataset:
                # Alternate order within a pair and across repeats to reduce order bias.
                order = (False, True) if (attempt + dataset.index(case)) % 2 else (True, False)
                for scheduled in order:
                    row = await run_batch(case, scheduled, attempt, root, provider, selected.model)
                    rows.append(row)
                    atomic_json(root / "results.json", rows)
                    # Only summarize completed pairs while a run is in progress.
                    counts = Counter(r["case_id"] for r in rows)
                    paired = [r for r in rows if counts[r["case_id"]] % 2 == 0]
                    if paired:
                        write_report(root, paired)
                    print(
                        f"{len(rows)}/{manifest['planned_attempts']} {case.id} "
                        f"{row['variant']} {attempt}: {row['success']} "
                        f"{row['duration_seconds']:.3f}s",
                        flush=True,
                    )
                    if row["error_type"] or row["fatal_provider_error"]:
                        raise RuntimeError(f"infrastructure/planner failure: {row['error_type']}")
    finally:
        await provider._client.close()
        manifest["completed_attempts"] = len(rows)
        manifest["run_status"] = (
            "completed" if len(rows) == manifest["planned_attempts"] else "interrupted"
        )
        atomic_json(root / "experiment.json", manifest)
    write_report(root, rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--output", default=".xclaw/evaluations/scheduling-20261030/component")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--slow-delay", type=float, default=2)
    parser.add_argument("--fast-delay", type=float, default=0.05)
    args = parser.parse_args()
    if args.repeats < 1 or any(
        not math.isfinite(v) or v <= 0 for v in (args.slow_delay, args.fast_delay)
    ):
        parser.error("positive finite delays/repeats required")
    logger.remove()
    logger.add(sys.stderr, level="WARNING")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
