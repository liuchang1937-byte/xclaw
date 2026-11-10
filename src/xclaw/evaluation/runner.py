"""Evaluate the final agent configuration and preserve every attempt, including failures."""

from __future__ import annotations

import argparse
import ast
import asyncio
import hashlib
import json
import math
import platform
import statistics
import subprocess
import sys
import time
import uuid
from dataclasses import asdict, dataclass
from fnmatch import fnmatchcase
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from loguru import logger

from xclaw.ai.context import ContextBudgetExceededError, ContextProtocolError
from xclaw.ai.provider import OpenAIProvider
from xclaw.ai.types import ChatMessage
from xclaw.coding import CodingAgent
from xclaw.coding.bash_tool import register_bash_tool
from xclaw.coding.file_tools import register_file_tools
from xclaw.core.execution import RunProcess
from xclaw.core.scheduling import SchedulingConfig
from xclaw.core.workspace import TaskWorkspace
from xclaw.evaluation.assessment import attach_assessment, summarize_dimensions
from xclaw.evaluation.datasets import balanced_tasks, select_tasks
from xclaw.evaluation.spec import (
    SCORER_VERSION,
    EvaluationSpec,
    SuiteSpec,
    digest,
    evaluation_hash,
)
from xclaw.evaluation.tasks import Task, builtin_tasks, load_tasks, validation_script
from xclaw.persistence import atomic_json
from xclaw.sandbox import DockerSandbox, ExecutionPolicy
from xclaw.sandbox.process import capture_process
from xclaw.server.config import Config

VARIANTS = {
    "summary_serial": ("summary", False),
    "summary_scheduled": ("summary", True),
    "truncate_serial": ("truncate", False),
    "truncate_scheduled": ("truncate", True),
}


@dataclass
class EvalSettings:
    model: str
    context_window: int = 8192
    max_tokens: int = 1024
    max_rounds: int = 12
    temperature: float = 0
    task_timeout: float = 180
    validation_timeout: float = 30
    safety_margin: int = 512
    image: str = "xclaw-sandbox:latest"
    max_parallel: int = 4
    planning_timeout: float = 10
    capture_content: bool = True


def source_versions(package: Path | None = None) -> dict:
    package = package or Path(__file__).resolve().parents[1]
    groups = {name: {} for name in ("runtime", "tools", "scorer", "prompts", "raw")}
    for path in sorted(package.glob("*.py")):
        content = path.read_text(encoding="utf-8")
        groups["raw"][path.name] = digest(content)
        groups["runtime"][path.name] = digest(ast.dump(ast.parse(content)))
    for folder in ("ai", "core", "sandbox", "coding", "evaluation"):
        for path in sorted((package / folder).rglob("*.py")):
            relative = path.relative_to(package).as_posix()
            content = path.read_text(encoding="utf-8")
            groups["raw"][relative] = digest(content)
            tree = ast.parse(content)
            # Record prompt edits independently so a declared prompt experiment remains comparable.
            for node in ast.walk(tree):
                if isinstance(node, ast.Assign) and any(
                    isinstance(target, ast.Name) and target.id.endswith("_PROMPT")
                    for target in node.targets
                ):
                    names = [t.id for t in node.targets if isinstance(t, ast.Name)]
                    groups["prompts"][relative + ":" + ",".join(names)] = ast.dump(node.value)
                    node.value = ast.Constant(value="[versioned prompt]")
            group = (
                "scorer"
                if folder == "evaluation"
                else (
                    "tools" if folder == "coding" and path.name != "coding_agent.py" else "runtime"
                )
            )
            groups[group][relative] = digest(ast.dump(tree))
    return {group: digest(files) for group, files in groups.items()}


def dependency_versions() -> dict:
    result = {}
    for name in ("xclaw", "openai", "httpx", "pydantic", "mcp", "jsonschema", "loguru", "pyyaml"):
        try:
            result[name] = version(name)
        except PackageNotFoundError:
            result[name] = "unavailable"
    return result


def _write_files(directory: Path, files: dict[str, str]):
    directory.mkdir(parents=True, exist_ok=True)
    for relative, content in files.items():
        target = directory / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


async def validate_docker(
    task: Task, workspace: TaskWorkspace, oracle: Path, settings: EvalSettings
):
    validator = DockerSandbox(workspace=str(workspace.work), image=settings.image, read_only=True)
    validator.configure_skills(oracle)
    try:
        result = await validator.run_process(
            RunProcess(
                argv=("python", "-I", "/opt/xclaw-skills/validate.py", "/workspace"),
                timeout=settings.validation_timeout,
                output_limit=16000,
            )
        )
        marker = f"XCLAW_VALIDATION_OK:{len(task.checks)}"
        return {
            "passed": result.ok and marker in result.stdout.splitlines(),
            "exit_code": result.exit_code,
            "timed_out": result.timed_out,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "infrastructure_error": result.exit_code == -1,
        }
    finally:
        await validator.stop()


async def run_case(
    task: Task,
    variant: str,
    attempt: int,
    root: Path,
    settings: EvalSettings,
    provider,
    *,
    validator=validate_docker,
) -> dict:
    spec = EvaluationSpec.model_validate(task.evaluation)
    suite = SuiteSpec.model_validate(task.suite)
    case = root / f"{task.id}-{variant}-{attempt}"
    case.mkdir(parents=True, exist_ok=False)
    atomic_json(
        case / "task.json",
        {
            "id": task.id,
            "category": task.category,
            "task_hash": task.fingerprint,
            "prompt": task.prompt,
            "history": task.history,
            "evaluation_requirements": task.evaluation_requirements,
            "expected_tool_groups": task.expected_tool_groups,
            "evaluation": spec.model_dump(),
            "evaluation_hash": evaluation_hash(spec),
            "suite": suite.model_dump(),
        },
    )
    seed, oracle = case / "seed", case / "oracle"
    _write_files(seed, {"README.md": "Evaluation fixture. Do not modify this file.\n", **task.seed})
    _write_files(oracle, {"validate.py": validation_script(task)})
    snapshot = await asyncio.to_thread(TaskWorkspace.create, seed, case / "tasks")
    strategy, scheduled = VARIANTS[variant]
    bot = CodingAgent(
        provider,
        model=settings.model,
        temperature=settings.temperature,
        max_tokens=settings.max_tokens,
        max_tool_rounds=settings.max_rounds,
        workspace_dir=str(snapshot.work),
        register_default_tools=False,
        execution_policy=ExecutionPolicy(
            max_timeout=30, write_paths=tuple(task.allowed_write_paths or task.seed)
        ),
        docker_image=settings.image,
        trace_dir=str(case / "traces"),
        enable_context_compression=True,
        context_strategy=strategy,
        context_window=settings.context_window,
        compression_safety_margin=settings.safety_margin,
        tool_scheduling=SchedulingConfig(
            enabled=scheduled,
            max_parallel=settings.max_parallel,
            planning_timeout=settings.planning_timeout,
        ),
    )
    register_file_tools(
        bot.tool_registry, workspace=str(snapshot.work), policy=bot.execution_policy
    )
    register_bash_tool(bot.tool_registry)
    bot.trace_capture_content = settings.capture_content
    bot.client.set_history([ChatMessage(**message) for message in task.history])
    row = {
        "task_id": task.id,
        "category": task.category,
        "task_hash": task.fingerprint,
        "variant": variant,
        "attempt": attempt,
        "artifact_directory": str(case),
        "settings": asdict(settings),
        "status": "error",
        "success": False,
        "evaluation_hash": evaluation_hash(spec),
        "suite": suite.model_dump(),
        "provenance": {
            "model": settings.model,
            "prompt_hash": digest(bot.client.system_prompt),
            "tools_hash": digest([d.model_dump() for d in bot.tool_registry.get_all_definitions()]),
            "sources": source_versions(),
            "python": platform.python_version(),
            "platform": platform.platform(),
            "image": settings.image,
            "dependencies": dependency_versions(),
        },
    }
    started = time.perf_counter()
    response = None
    try:
        async with asyncio.timeout(settings.task_timeout):
            response = await bot.run(
                task.prompt
                + "\n只允许修改这些路径或模式："
                + str(task.allowed_write_paths or list(task.seed))
            )
        row["status"] = bot.last_run_status
        (case / "response.txt").write_text(response, encoding="utf-8")
        # Validators may check the final answer without mounting it in the agent workspace.
        (oracle / "response.txt").write_text(response, encoding="utf-8")
    except TimeoutError:
        row["status"] = "timeout"
    except ContextBudgetExceededError:
        row["status"] = "context_budget"
    except ContextProtocolError:
        row["status"] = "context_protocol"
    except asyncio.CancelledError:
        row["status"] = "cancelled"
        raise
    except Exception as exc:
        row["status"] = "model_or_runtime_error"
        row["error_type"] = type(exc).__name__
        row["http_status"] = getattr(exc, "status_code", None)
        if row["http_status"] == 402:
            row["status"] = "provider_balance_unavailable"
        elif row["http_status"] in {400, 401, 403, 404, 405, 410, 422}:
            row["status"] = "provider_configuration_error"
    finally:
        row["duration_seconds"] = time.perf_counter() - started
        row["metrics"] = bot.last_trace.metrics() if bot.last_trace else {}
        try:
            await bot.sandbox_router.stop()
        except Exception as exc:
            row["status"] = "cleanup_error"
            row["cleanup_error_type"] = type(exc).__name__
        attach_assessment(row, spec, bot.last_trace.events if bot.last_trace else [], response)
        atomic_json(case / "result.json", row)
    if row["status"] in {"provider_balance_unavailable", "provider_configuration_error"}:
        row["validation_skipped_reason"] = "provider_unavailable"
        row["diff"] = str(snapshot.export_diff())
        row["changes"] = snapshot.changes()
        atomic_json(case / "result.json", row)
        return row
    try:
        row["diff"] = str(snapshot.export_diff())
        row["changes"] = snapshot.changes()
        row["constraints_passed"] = all(
            any(
                fnmatchcase(change["path"], scope)
                for scope in (task.allowed_write_paths or task.seed)
            )
            for change in row["changes"]
        )
        # Run the independent checks even if the model ran out of rounds, but do not count
        # interrupted/failed runtime executions as a completed task.
        validation_started = time.perf_counter()
        validation = await validator(task, snapshot, oracle, settings)
        row["validation_seconds"] = time.perf_counter() - validation_started
        atomic_json(case / "validation.json", validation)
        row["validation_passed"] = validation["passed"]
        row["success"] = (
            row["status"] == "completed"
            and validation["passed"]
            and row["constraints_passed"]
            and not validation.get("infrastructure_error")
        )
        if validation.get("infrastructure_error"):
            row["status"] = "validation_infrastructure_error"
        elif row["status"] == "completed" and not row["constraints_passed"]:
            row["status"] = "constraint_violation"
        elif row["status"] == "completed" and not validation["passed"]:
            row["status"] = "acceptance_failed"
    except Exception as exc:
        row["status"] = "validation_error"
        row["error_type"] = type(exc).__name__
    finally:
        attach_assessment(row, spec, bot.last_trace.events if bot.last_trace else [], response)
        atomic_json(case / "result.json", row)
    if bot.last_trace:
        bot.last_trace.emit(
            "evaluation.finished",
            success=row["success"],
            status=row["status"],
            scorer_version=SCORER_VERSION,
            evaluation_hash=evaluation_hash(spec),
        )
    return row


def summarize(rows: list[dict]) -> dict:
    summary = {}
    for variant in sorted({row["variant"] for row in rows}):
        selected = [r for r in rows if r["variant"] == variant]
        durations = sorted(r["duration_seconds"] for r in selected)
        metrics = [r.get("metrics", {}) for r in selected]
        failures = {}
        for row in selected:
            if not row["success"]:
                failures[row["status"]] = failures.get(row["status"], 0) + 1
        summary[variant] = {
            "attempts": len(selected),
            "tasks": len({r["task_id"] for r in selected}),
            "successes": sum(r["success"] for r in selected),
            "success_rate": sum(r["success"] for r in selected) / len(selected),
            "median_seconds": statistics.median(durations),
            "p95_seconds": durations[math.ceil(len(durations) * 0.95) - 1]
            if len(durations) >= 20
            else None,
            "mean_reported_tokens": statistics.mean(m.get("reported_tokens", 0) for m in metrics),
            "usage_complete": all(m.get("usage_complete", False) for m in metrics),
            "mean_model_calls": statistics.mean(m.get("model_calls", 0) for m in metrics),
            "mean_tool_calls": statistics.mean(m.get("tool_calls", 0) for m in metrics),
            "failures": failures,
            "by_category": {
                category: {
                    "attempts": sum(r["category"] == category for r in selected),
                    "successes": sum(r["success"] for r in selected if r["category"] == category),
                }
                for category in sorted({r["category"] for r in selected})
            },
        }
    return summary


def write_report(root: Path, rows: list[dict]) -> Path:
    summary = summarize(rows)
    atomic_json(root / "summary.json", summary)
    dimensions = summarize_dimensions(rows)
    atomic_json(root / "dimensions.json", dimensions)
    lines = [
        "# XClaw 任务评测\n",
        "成功需要执行完成、独立验收通过、文件约束及硬性评测规则满足。\n",
        "所有辅助模型调用和重试均计入调用次数。Token 为服务端已报告用量；缺失时仅为下界。\n",
        "| 方案 | 次数 | 成功率 | 耗时中位数(s) | P95(s) | 平均已报告 Token | 用量完整 |",
        "|---|---:|---:|---:|---:|---:|---|",
    ]
    for variant, stats in summary.items():
        p95 = f"{stats['p95_seconds']:.2f}" if stats["p95_seconds"] is not None else "样本不足20"
        lines.append(
            f"| {variant} | {stats['attempts']} | {stats['success_rate']:.1%} | "
            f"{stats['median_seconds']:.2f} | {p95} | {stats['mean_reported_tokens']:.0f} | "
            f"{stats['usage_complete']} |"
        )
    lines += [
        "\n## 四维评测（任务等权，重复运行先在任务内平均）\n",
        "缺失指标为 N/A；答案准确率基于标注规则，用户满意度需要独立反馈。"
        "工具选择准确率需要逐调用标注，不能以工具执行成功率替代。\n",
        "| 方案 | 任务成功率 | 平均步数 | 对抗通过率 | 工具选择准确率 | "
        "无效步数占比 | 重复动作率 |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]

    def show(metric, percent=False):
        value = metric["value"] if isinstance(metric, dict) else metric
        if value is None:
            return "N/A"
        rendered = f"{value:.1%}" if percent else f"{value:.2f}"
        if isinstance(metric, dict):
            rendered += f" ({metric['observed_attempts']}/{metric['total_attempts']})"
        return rendered

    for variant, stats in dimensions.items():
        dims = stats["dimensions"]
        lines.append(
            f"| {variant} | {show(dims['effectiveness']['success_rate'], True)} | "
            f"{show(dims['efficiency']['steps'])} | "
            f"{show(dims['robustness']['adversarial_passed'], True)} | "
            f"{show(dims['process_quality']['tool_selection_accuracy'], True)} | "
            f"{show(dims['process_quality']['invalid_step_ratio'], True)} | "
            f"{show(dims['process_quality']['repeated_action_ratio'], True)} |"
        )
    lines += [
        "\n完整分母、覆盖率、场景/能力/留出集分层、失败归因和优化候选见 "
        "[dimensions.json](dimensions.json)。",
        "\n## 失败记录\n",
    ]
    lines += [
        f"- {r['task_id']} / {r['variant']} / 第 {r['attempt']} 次：{r['status']}；"
        f"[产物]({Path(r['artifact_directory']).relative_to(root).as_posix()}/result.json)"
        for r in rows
        if not r["success"]
    ]
    if not any(not r["success"] for r in rows):
        lines.append("本次运行没有失败记录。")
    lines.append("\n小样本与合成任务只用于回归和定位问题，不能推断真实仓库任务的整体效果。")
    path = root / "report.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def self_check(root: Path, tasks: list[Task]) -> dict:
    """Run only curated reference/seed code on host; no model output is ever run here."""
    results = []
    for task in tasks:
        directory = root / task.id
        seed, reference, oracle = directory / "seed", directory / "reference", directory / "oracle"
        _write_files(seed, task.seed)
        _write_files(reference, task.reference)
        _write_files(oracle, {"validate.py": validation_script(task)})
        row = {"id": task.id, "task_hash": task.fingerprint}
        for name, folder in (("seed", seed), ("reference", reference)):
            (oracle / "response.txt").write_text(
                task.reference_answer or "" if name == "reference" else "", encoding="utf-8"
            )
            result = subprocess.run(
                [
                    sys.executable,
                    "-I",
                    str((oracle / "validate.py").resolve()),
                    str(folder.resolve()),
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
            )
            row[name + "_passes"] = result.returncode == 0
            (directory / f"{name}.log").write_text(result.stdout + result.stderr, encoding="utf-8")
        results.append(row)
    report = {
        "kind": "fixture_self_check",
        "model_called": False,
        "tasks": results,
        "passed": all(not r["seed_passes"] and r["reference_passes"] for r in results),
    }
    atomic_json(root / "self_check.json", report)
    return report


async def run_suite(args, tasks):
    # Read only the requested config; never silently fall back to a different provider.
    config = Config.from_yaml(args.config)
    provider_config = next((p for p in config.providers if p.name == config.default_provider), None)
    if provider_config is None or not provider_config.api_key:
        raise ValueError("默认 Provider 不存在或 API key 为空")
    context_window = args.context_window or min(provider_config.context_window, 8192)
    if context_window > provider_config.context_window:
        raise ValueError("评测窗口不能大于 Provider 配置窗口")
    settings = EvalSettings(
        model=provider_config.model,
        context_window=context_window,
        max_tokens=args.max_tokens,
        task_timeout=args.timeout,
        image=config.agent.docker_image,
        max_rounds=args.max_rounds,
        capture_content=not args.metadata_only,
    )
    if settings.context_window <= settings.max_tokens + settings.safety_margin:
        raise ValueError("评测上下文窗口不足")
    try:
        image = await capture_process(
            ["docker", "image", "inspect", settings.image, "--format", "{{.Id}}"],
            timeout=30,
            output_limit=2000,
        )
    except OSError as exc:
        raise RuntimeError("Docker 不可用，尚未调用模型") from exc
    if not image.ok:
        raise RuntimeError("Docker 或执行镜像不可用，尚未调用模型；请先构建 Dockerfile.sandbox")
    # Use the inspected immutable image, even if the tag changes while this suite is running.
    settings.image = image.stdout.strip()
    root = Path(args.output).resolve() / uuid.uuid4().hex
    root.mkdir(parents=True)
    dataset_hash = hashlib.sha256("".join(t.fingerprint for t in tasks).encode()).hexdigest()
    experiment = {
        "kind": "live_model",
        "dataset_hash": dataset_hash,
        "evaluation_hash": digest(
            [evaluation_hash(EvaluationSpec.model_validate(t.evaluation)) for t in tasks]
        ),
        "suite": SuiteSpec.model_validate(tasks[0].suite).model_dump(),
        "scorer_version": SCORER_VERSION,
        "source_versions": source_versions(),
        "dependencies": dependency_versions(),
        "mode": args.mode,
        "split": args.split,
        "selection": "subset" if args.tasks else args.mode,
        "tasks": [t.id for t in tasks],
        "variants": args.variants,
        "repeats": args.repeats,
        "settings": asdict(settings),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "provider_name": provider_config.name,
        "auxiliary_thinking": provider_config.auxiliary_thinking,
        "image_id": image.stdout.strip(),
        "planned_attempts": len(tasks) * len(args.variants) * args.repeats,
        "run_status": "running",
    }
    atomic_json(root / "experiment.json", experiment)
    provider = OpenAIProvider(
        provider_config.api_key,
        provider_config.base_url,
        auxiliary_thinking=provider_config.auxiliary_thinking,
    )
    rows = []
    concurrency = args.concurrency
    experiment["concurrency"] = concurrency
    atomic_json(root / "experiment.json", experiment)

    async def execute_one(task, variant, attempt):
        try:
            row = await run_case(task, variant, attempt, root, settings, provider)
        except asyncio.CancelledError:
            case = root / f"{task.id}-{variant}-{attempt}"
            if (case / "result.json").is_file():
                rows.append(json.loads((case / "result.json").read_text(encoding="utf-8")))
                atomic_json(root / "results.json", rows)
                write_report(root, rows)
            raise
        except Exception as exc:
            case = root / f"{task.id}-{variant}-{attempt}"
            row = {
                "task_id": task.id,
                "category": task.category,
                "task_hash": task.fingerprint,
                "evaluation_hash": evaluation_hash(EvaluationSpec.model_validate(task.evaluation)),
                "suite": SuiteSpec.model_validate(task.suite).model_dump(),
                "variant": variant,
                "attempt": attempt,
                "success": False,
                "status": "setup_error",
                "duration_seconds": 0,
                "metrics": {},
                "artifact_directory": str(case),
                "error_type": type(exc).__name__,
            }
            attach_assessment(row, EvaluationSpec.model_validate(task.evaluation), [], None)
            atomic_json(case / "result.json", row)
        rows.append(row)
        atomic_json(root / "results.json", rows)
        write_report(root, rows)
        print(
            f"{len(rows)}/{experiment['planned_attempts']} "
            f"{task.id} {variant} {attempt}: {row['status']}",
            flush=True,
        )
        if row["status"] in {
            "validation_infrastructure_error",
            "cleanup_error",
            "provider_balance_unavailable",
            "provider_configuration_error",
        }:
            raise RuntimeError(f"基础设施故障，停止后续模型调用。已保存结果：{root}")

    jobs = []
    for attempt in range(1, args.repeats + 1):
        variants = (
            args.variants[(attempt - 1) % len(args.variants) :]
            + args.variants[: (attempt - 1) % len(args.variants)]
        )
        jobs.extend((task, variant, attempt) for task in tasks for variant in variants)
    try:
        await execute_jobs(jobs, execute_one, concurrency)
    finally:
        await provider._client.close()
        experiment["completed_attempts"] = len(rows)
        experiment["run_status"] = (
            "completed" if len(rows) == experiment["planned_attempts"] else "interrupted"
        )
        if any(r["status"] == "provider_balance_unavailable" for r in rows):
            experiment["run_status"] = "blocked_provider_balance"
        elif any(r["status"] == "provider_configuration_error" for r in rows):
            experiment["run_status"] = "blocked_provider_configuration"
        atomic_json(root / "experiment.json", experiment)
    print(root / "report.md")
    if args.baseline:
        from xclaw.evaluation.audit import check_gate

        gate = check_gate(Path(args.baseline), root, change=args.change)
        atomic_json(root / "gate.json", gate)
        if not gate["passed"]:
            raise SystemExit(2)


async def execute_jobs(jobs, execute, concurrency):
    """Bound independent task attempts; task-scoped ContextVars isolate their telemetry."""
    queue = asyncio.Queue()
    for job in jobs:
        queue.put_nowait(job)

    async def worker():
        while True:
            try:
                job = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            try:
                await execute(*job)
            finally:
                queue.task_done()

    async with asyncio.TaskGroup() as group:
        for _ in range(concurrency):
            group.create_task(worker())


def main():
    parser = argparse.ArgumentParser(description="XClaw 最终方案端到端任务评测（摘要＋依赖调度）")
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--self-check", action="store_true")
    parser.add_argument("--suite", help="使用自定义仓库任务 JSON 清单")
    parser.add_argument("--dataset", choices=["legacy", "balanced"], default="legacy")
    parser.add_argument("--mode", choices=["full", "smoke"], default="full")
    parser.add_argument("--split", choices=["evaluation", "holdout"], default="evaluation")
    parser.add_argument("--change", choices=["model", "prompt", "tools", "runtime", "scheduling"])
    parser.add_argument("--baseline", help="完整基线实验目录；门禁失败退出 2")
    parser.add_argument("--metadata-only", action="store_true", help="关闭评测 Trace 的正文捕获")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--output", default=".xclaw/evaluations")
    parser.add_argument("--tasks", nargs="+")
    parser.add_argument(
        "--variants",
        nargs="+",
        choices=list(VARIANTS),
        default=["summary_scheduled"],
        help="默认只评测最终方案 summary_scheduled（摘要＋依赖调度）",
    )
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument(
        "--concurrency",
        type=int,
        default=1,
        choices=range(1, 5),
        help="独立任务并发数（所有方案共用，默认1）",
    )
    parser.add_argument("--context-window", type=int)
    parser.add_argument("--max-tokens", type=int, default=1024)
    parser.add_argument("--max-rounds", type=int, default=12)
    parser.add_argument("--timeout", type=float, default=180)
    args = parser.parse_args()
    if len(set(args.variants)) != len(args.variants):
        parser.error("variants 不能重复")
    if args.change and (args.mode != "full" or args.tasks or args.split != "evaluation"):
        parser.error("模型/Prompt/工具等变更必须运行 evaluation split 的全量评测")
    if args.baseline and (args.mode != "full" or args.tasks or args.split != "evaluation"):
        parser.error("回归门禁需要 evaluation split 的全量评测")
    tasks = (
        load_tasks(args.suite)
        if args.suite
        else (balanced_tasks() if args.dataset == "balanced" else builtin_tasks())
    )
    tasks = select_tasks(tasks, mode=args.mode, split=args.split)
    if args.tasks:
        unknown = set(args.tasks) - {t.id for t in tasks}
        if unknown:
            parser.error("未知任务: " + ", ".join(sorted(unknown)))
        tasks = [task for task in tasks if task.id in args.tasks]
    if args.list:
        for task in tasks:
            spec = EvaluationSpec.model_validate(task.evaluation)
            print(
                f"{task.id}\t{task.category}\t{spec.scenario}\t{spec.task_type}\t"
                f"{len(task.checks)} checks"
            )
        return
    if (
        args.repeats < 1
        or args.max_tokens < 1
        or args.max_rounds < 1
        or not math.isfinite(args.timeout)
        or args.timeout <= 0
    ):
        parser.error("repeats、max-tokens、timeout 必须为正数")
    logger.remove()
    logger.add(sys.stderr, level="WARNING")
    if args.self_check:
        if args.suite:
            parser.error("self-check 仅用于内置参考任务；自定义任务请独立验证其验收脚本")
        root = Path(args.output).resolve() / ("self-check-" + uuid.uuid4().hex)
        report = self_check(root, tasks)
        print(root / "self_check.json")
        if not report["passed"]:
            raise SystemExit(1)
    else:
        asyncio.run(run_suite(args, tasks))


if __name__ == "__main__":
    main()
