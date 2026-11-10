"""Dependency scheduling on real file tools and fixed simulated query services.

No main-model generation: the live model only schedules fixed tool calls. Gold
dependencies and output checks are held outside planner messages. Each attempt
uses an isolated workspace. Synthetic delays are recorded, not production I/O.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import inspect
import json
import math
import platform
import statistics
import sys
import time
import uuid
from collections import Counter
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field
from functools import wraps
from pathlib import Path

from loguru import logger

from xclaw.ai.provider import OpenAIProvider
from xclaw.ai.types import ChatMessage, ToolCall, ToolResult
from xclaw.coding import CodingAgent
from xclaw.coding.file_tools import register_file_tools
from xclaw.core.execution import Capability
from xclaw.core.scheduling import SchedulingConfig, tool_failed
from xclaw.core.tool import Tool
from xclaw.evaluation.runner import dependency_versions, source_versions
from xclaw.evaluation.spec import digest
from xclaw.im.delivery import collect_deliveries, register_delivery_tool
from xclaw.persistence import atomic_json
from xclaw.server.config import Config
from xclaw.telemetry import RunTrace, activate, emit


@dataclass
class Call:
    id: str
    tool: str
    arguments: dict


@dataclass
class Scenario:
    id: str
    category: str
    context: str
    calls: list[Call]
    seed: dict[str, str] = field(default_factory=dict)
    dependencies: dict[str, list[str]] = field(default_factory=dict)
    result_contains: dict[str, str] = field(default_factory=dict)
    final_files: dict[str, str | None] = field(default_factory=dict)
    failed: list[str] = field(default_factory=list)
    injected_failures: dict[str, str] = field(default_factory=dict)
    blocked: list[str] = field(default_factory=list)
    deferred: list[str] = field(default_factory=list)
    independent_groups: list[list[str]] = field(default_factory=list)
    deliveries: list[str] = field(default_factory=list)


def call(identifier, tool, **arguments):
    return Call(identifier, tool, arguments)


def scenarios():
    read = lambda i, path: call(i, "read_file", path=path)  # noqa: E731
    write = lambda i, path, content: call(i, "write_file", path=path, content=content)  # noqa: E731
    edit = lambda i, old, new: call(  # noqa: E731
        i, "edit_file", path="config.txt", old_string=old, new_string=new
    )
    return [
        Scenario(
            "independent_four_files",
            "independent",
            "查看四份不同模块的配置文件。",
            [read(str(i), f"module{i}.txt") for i in range(4)],
            seed={f"module{i}.txt": f"module={i}" for i in range(4)},
            result_contains={str(i): f"module={i}" for i in range(4)},
            independent_groups=[[str(i) for i in range(4)]],
        ),
        Scenario(
            "independent_eight_files",
            "independent",
            "收集八个模块的配置，各模块互不影响。",
            [read(str(i), f"module{i}.txt") for i in range(8)],
            seed={f"module{i}.txt": f"module={i}" for i in range(8)},
            result_contains={str(i): f"module={i}" for i in range(8)},
            independent_groups=[[str(i) for i in range(8)]],
        ),
        Scenario(
            "same_file_two_reads",
            "independent",
            "两个检查都只需要查看同一份现有配置，没有修改。",
            [read("left", "config.txt"), read("right", "config.txt")],
            seed={"config.txt": "revision=1"},
            result_contains={"left": "revision=1", "right": "revision=1"},
            independent_groups=[["left", "right"]],
        ),
        Scenario(
            "write_then_read_reversed",
            "file_order",
            "创建报告后查看刚生成的内容。",
            [read("inspect", "report.txt"), write("create", "report.txt", "report=v2")],
            dependencies={"inspect": ["create"]},
            result_contains={"inspect": "report=v2"},
            final_files={"report.txt": "report=v2"},
        ),
        Scenario(
            "write_then_search_reversed",
            "file_order",
            "生成日志后搜索其中的订单编号。",
            [
                call(
                    "search",
                    "search_in_files",
                    directory=".",
                    query="ORDER_314",
                    file_pattern="*.log",
                ),
                write("generate", "orders.log", "ORDER_314 paid"),
            ],
            dependencies={"search": ["generate"]},
            result_contains={"search": "ORDER_314 paid"},
            final_files={"orders.log": "ORDER_314 paid"},
        ),
        Scenario(
            "write_then_list_reversed",
            "file_order",
            "创建导出文件，然后确认输出目录出现了这个文件。",
            [
                call("list", "list_directory", path="exports"),
                write("export", "exports/orders.csv", "id,total\n314,88"),
            ],
            dependencies={"list": ["export"]},
            result_contains={"list": "orders.csv"},
            final_files={"exports/orders.csv": "id,total\n314,88"},
        ),
        Scenario(
            "edit_then_read_reversed",
            "file_order",
            "把配置的版本从1改为2，查看修改后的配置。",
            [read("inspect", "config.txt"), edit("update", "revision=1", "revision=2")],
            seed={"config.txt": "revision=1"},
            dependencies={"inspect": ["update"]},
            result_contains={"inspect": "revision=2"},
            final_files={"config.txt": "revision=2"},
        ),
        Scenario(
            "two_edits_chain_reversed",
            "file_order",
            "配置依次经过draft、review、approved三个状态，最后查看审批后的内容。",
            [
                read("inspect", "config.txt"),
                edit("approve", "status=review", "status=approved"),
                edit("review", "status=draft", "status=review"),
            ],
            seed={"config.txt": "status=draft"},
            dependencies={"approve": ["review"], "inspect": ["approve"]},
            result_contains={"inspect": "status=approved"},
            final_files={"config.txt": "status=approved"},
        ),
        Scenario(
            "snapshot_update_snapshot",
            "file_order",
            "保留更新前的旧配置内容，将版本改为2，再查看更新后的内容。before是旧快照，after是新快照。",
            [
                read("after", "config.txt"),
                write("update", "config.txt", "revision=2"),
                read("before", "config.txt"),
            ],
            seed={"config.txt": "revision=1"},
            dependencies={"update": ["before"], "after": ["update"]},
            result_contains={"before": "revision=1", "after": "revision=2"},
            final_files={"config.txt": "revision=2"},
        ),
        Scenario(
            "overwrite_stale_report",
            "file_order",
            "已有报告已过期，覆盖为本期报告，查看本期内容。",
            [read("inspect", "report.txt"), write("refresh", "report.txt", "period=2026-10")],
            seed={"report.txt": "period=2026-09"},
            dependencies={"inspect": ["refresh"]},
            result_contains={"inspect": "period=2026-10"},
            final_files={"report.txt": "period=2026-10"},
        ),
        Scenario(
            "query_fork_join",
            "query_order",
            "查询客户资料和折扣政策，使用两项最新查询结果检查订单资格。参数都已知。",
            [
                call("evaluate", "evaluate_order", customer_id="C314"),
                call("customer", "query_customer", customer_id="C314"),
                call("policy", "query_policy", policy_id="P2026"),
            ],
            dependencies={"evaluate": ["customer", "policy"]},
            result_contains={"evaluate": '"eligible": true'},
            independent_groups=[["customer", "policy"]],
        ),
        Scenario(
            "query_join_with_independent_read",
            "query_order",
            "用客户和政策查询结果检查订单，同时查看无需这些数据的公告文件。",
            [
                call("evaluate", "evaluate_order", customer_id="C314"),
                read("notice", "notice.txt"),
                call("policy", "query_policy", policy_id="P2026"),
                call("customer", "query_customer", customer_id="C314"),
            ],
            seed={"notice.txt": "maintenance=none"},
            dependencies={"evaluate": ["customer", "policy"]},
            result_contains={"evaluate": '"eligible": true', "notice": "maintenance=none"},
            independent_groups=[["notice", "customer", "policy"]],
        ),
        Scenario(
            "query_check_render_chain",
            "query_order",
            "加载本次客户资料，用该资料完成风控校验，再基于本次校验结果渲染摘要。参数已知。",
            [
                call("render", "render_summary", customer_id="C314"),
                call("validate", "validate_customer", customer_id="C314"),
                call("customer", "query_customer", customer_id="C314"),
            ],
            dependencies={"validate": ["customer"], "render": ["validate"]},
            result_contains={"render": "approved customer C314"},
        ),
        Scenario(
            "write_check_deliver",
            "delivery",
            "生成最终报告，检查内容正确后作为附件交付。所有路径与预期内容已知。",
            [
                call("deliver", "deliver_file", path="report.txt"),
                call("check", "check_file", path="report.txt", expected="total=88"),
                write("create", "report.txt", "total=88"),
            ],
            dependencies={"check": ["create"], "deliver": ["create", "check"]},
            result_contains={"check": '"valid": true'},
            final_files={"report.txt": "total=88"},
            deliveries=["report.txt"],
        ),
        Scenario(
            "two_reports_independent_chains",
            "delivery",
            "分别生成并检查a、b两份报告，通过各自检查后交付。两条报告流程独立。",
            [
                call("send_b", "deliver_file", path="b.txt"),
                call("check_a", "check_file", path="a.txt", expected="a=1"),
                write("write_b", "b.txt", "b=2"),
                call("send_a", "deliver_file", path="a.txt"),
                call("check_b", "check_file", path="b.txt", expected="b=2"),
                write("write_a", "a.txt", "a=1"),
            ],
            dependencies={
                "check_a": ["write_a"],
                "send_a": ["check_a"],
                "check_b": ["write_b"],
                "send_b": ["check_b"],
            },
            final_files={"a.txt": "a=1", "b.txt": "b=2"},
            deliveries=["a.txt", "b.txt"],
        ),
        Scenario(
            "failed_write_blocks_read",
            "failure",
            "生成报告后读取新内容；生成失败时不要读取旧报告。公告读取独立。",
            [
                read("inspect", "report.txt"),
                read("notice", "notice.txt"),
                write("create", "report.txt", "new report"),
            ],
            seed={"report.txt": "stale report", "notice.txt": "all systems normal"},
            dependencies={"inspect": ["create"]},
            failed=["create"],
            blocked=["inspect"],
            injected_failures={"create": "error_prefix"},
            final_files={"report.txt": "stale report"},
            result_contains={"notice": "all systems normal"},
        ),
        Scenario(
            "failed_check_blocks_delivery",
            "failure",
            "先检查已有报告是否满足本次总额，检查通过后才交付；不合格报告禁止发送。公告读取独立。",
            [
                call("deliver", "deliver_file", path="report.txt"),
                read("notice", "notice.txt"),
                call("check", "check_file", path="report.txt", expected="total=88"),
            ],
            seed={"report.txt": "total=0", "notice.txt": "all systems normal"},
            dependencies={"deliver": ["check"]},
            failed=["check"],
            blocked=["deliver"],
            result_contains={"notice": "all systems normal"},
            final_files={"report.txt": "total=0"},
        ),
        Scenario(
            "query_failure_keeps_independent_branch",
            "failure",
            "查询客户和政策后检查订单；任何必要查询失败都不要检查订单。公告查询独立且需要完成。",
            [
                call("evaluate", "evaluate_order", customer_id="C314"),
                read("notice", "notice.txt"),
                call("customer", "query_customer", customer_id="C314"),
                call("policy", "query_policy", policy_id="P2026"),
            ],
            seed={"notice.txt": "all systems normal"},
            dependencies={"evaluate": ["customer", "policy"]},
            failed=["customer"],
            blocked=["evaluate"],
            injected_failures={"customer": "exception"},
            result_contains={"notice": "all systems normal", "policy": "P2026"},
        ),
        Scenario(
            "transitive_failure_chain",
            "failure",
            "加载客户后校验，再根据校验结果生成摘要。上游失败时下游全部停止，公告仍需读取。",
            [
                call("render", "render_summary", customer_id="C314"),
                call("validate", "validate_customer", customer_id="C314"),
                read("notice", "notice.txt"),
                call("customer", "query_customer", customer_id="C314"),
            ],
            seed={"notice.txt": "all systems normal"},
            dependencies={"validate": ["customer"], "render": ["validate"]},
            failed=["customer"],
            blocked=["validate", "render"],
            injected_failures={"customer": "error_prefix"},
            result_contains={"notice": "all systems normal"},
        ),
        Scenario(
            "missing_file_does_not_cancel_other_read",
            "failure",
            "分别查看两个独立文件；一个缺失不应取消另一个读取。",
            [read("missing", "absent.txt"), read("good", "notice.txt")],
            seed={"notice.txt": "all systems normal"},
            failed=["missing"],
            result_contains={"good": "all systems normal"},
            independent_groups=[["missing", "good"]],
        ),
        Scenario(
            "dynamic_record_id_deferred",
            "dynamic_parameters",
            "查找订单的实际记录ID，再用查找得到的ID读取详情。UNKNOWN是占位符，实际ID尚未知。公告独立。",
            [
                call("detail", "get_record", record_id="UNKNOWN"),
                read("notice", "notice.txt"),
                call("find", "find_record", query="order 314"),
            ],
            seed={"notice.txt": "all systems normal"},
            deferred=["detail"],
            result_contains={"find": "record-314", "notice": "all systems normal"},
            independent_groups=[["notice", "find"]],
        ),
        Scenario(
            "dynamic_record_id_known",
            "query_order",
            "记录ID已经由用户明确给出为record-314。查找用于审计核对，详情可用已知ID直接读取，两项互不依赖。",
            [
                call("detail", "get_record", record_id="record-314"),
                call("find", "find_record", query="order 314"),
            ],
            result_contains={"detail": '"total": 88', "find": "record-314"},
            independent_groups=[["detail", "find"]],
        ),
        Scenario(
            "two_file_updates_and_reads",
            "file_order",
            "把a、b两份配置分别更新为新版本，然后分别读取更新后的内容。文件之间没有依赖。",
            [
                read("read_b", "b.txt"),
                write("write_a", "a.txt", "a=new"),
                read("read_a", "a.txt"),
                write("write_b", "b.txt", "b=new"),
            ],
            seed={"a.txt": "a=old", "b.txt": "b=old"},
            dependencies={"read_a": ["write_a"], "read_b": ["write_b"]},
            result_contains={"read_a": "a=new", "read_b": "b=new"},
            final_files={"a.txt": "a=new", "b.txt": "b=new"},
        ),
        Scenario(
            "create_edit_check_fanout",
            "file_order",
            "创建draft配置，修改为approved，再分别读取并校验最终内容。查看与校验互不依赖。",
            [
                call("check", "check_file", path="config.txt", expected="status=approved"),
                read("inspect", "config.txt"),
                edit("approve", "status=draft", "status=approved"),
                write("create", "config.txt", "status=draft"),
            ],
            dependencies={"approve": ["create"], "check": ["approve"], "inspect": ["approve"]},
            result_contains={"inspect": "status=approved", "check": '"valid": true'},
            final_files={"config.txt": "status=approved"},
            independent_groups=[["check", "inspect"]],
        ),
    ]


def ancestors(plan):
    """Transitive closure; equivalent legal graphs need not have identical edges."""
    parents = {s["id"]: set(s["depends_on"]) for s in plan.get("steps", [])}
    for _ in range(len(parents)):
        expanded = {
            key: deps | set().union(*(parents.get(d, set()) for d in deps))
            for key, deps in parents.items()
        }
        if expanded == parents:
            break
        parents = expanded
    return parents


def install_queries(bot, workspace):
    cache = {}

    async def query_customer(customer_id: str):
        cache["customer"] = customer_id
        return {"customer_id": customer_id, "active": True}

    async def query_policy(policy_id: str):
        cache["policy"] = policy_id
        return {"policy_id": policy_id, "discount": 20}

    async def evaluate_order(customer_id: str):
        if cache.get("customer") != customer_id or "policy" not in cache:
            return "错误: 本次客户或政策查询结果尚未就绪"
        return {"eligible": True}

    async def validate_customer(customer_id: str):
        if cache.get("customer") != customer_id:
            return "错误: 本次客户资料尚未就绪"
        cache["validated"] = customer_id
        return {"approved": True}

    async def render_summary(customer_id: str):
        if cache.get("validated") != customer_id:
            return "错误: 本次风控校验尚未完成"
        return f"approved customer {customer_id}"

    async def find_record(query: str):
        return {"record_id": "record-314", "query": query}

    async def get_record(record_id: str):
        if record_id != "record-314":
            return "错误: 记录ID无效"
        return {"record_id": record_id, "total": 88}

    async def check_file(path: str, expected: str):
        target = workspace / path
        if not target.is_file() or target.read_text(encoding="utf-8") != expected:
            return "错误: 文件内容与预期不一致"
        return {"valid": True}

    definitions = [
        (query_customer, "查询客户资料并刷新本次查询结果缓存。"),
        (query_policy, "查询折扣政策并刷新本次查询结果缓存。"),
        (evaluate_order, "使用本次客户及政策查询结果缓存检查订单资格，不自行重新查询。"),
        (validate_customer, "使用本次客户查询结果缓存进行风控校验，并保存本次校验结果。"),
        (render_summary, "读取本次风控校验结果缓存，生成客户摘要，不重新查询或校验。"),
        (find_record, "按查询条件查找订单，返回实际的record_id。"),
        (get_record, "按一个确定且有效的record_id读取订单详情，不进行搜索。"),
        (check_file, "检查文件当前的完整文本是否与expected一致，一致返回valid=true，不修改文件。"),
    ]
    for func, description in definitions:
        bot.register_tool(
            Tool(
                func.__name__,
                description,
                func,
                capabilities=frozenset({Capability.FILE_READ}),
                parallel_safe=True,
                path_parameters=("path",) if func is check_file else (),
            )
        )


async def run_case(case, scheduled, attempt, root, provider, model, *, delay=0.02):
    variant = "dag" if scheduled else "serial"
    directory = root / f"{case.id}-{variant}-{attempt}"
    workspace = directory / "workspace"
    workspace.mkdir(parents=True, exist_ok=False)
    for name, content in case.seed.items():
        target = workspace / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    atomic_json(directory / "scenario.json", asdict(case))
    bot = CodingAgent(
        provider,
        model=model,
        workspace_dir=str(workspace),
        register_default_tools=False,
        tool_scheduling=SchedulingConfig(enabled=scheduled, max_parallel=4),
    )
    register_file_tools(bot.tool_registry, workspace=str(workspace))
    register_delivery_tool(bot.tool_registry)
    install_queries(bot, workspace)
    current_call = ContextVar("probe_call")
    spans, active, violations = {}, set(), []
    peak = 0

    def instrument(tool):
        original = tool.func

        @wraps(original)
        async def observed(*args, **kwargs):
            nonlocal peak
            identifier = current_call.get()
            exclusive = not tool.supports_parallel
            if exclusive and active or any(spans[key]["exclusive"] for key in active):
                violations.append({"kind": "exclusive_overlap", "call": identifier})
            spans[identifier] = {
                "started": time.perf_counter(),
                "ended": None,
                "ok": False,
                "exclusive": exclusive,
            }
            active.add(identifier)
            peak = max(peak, len(active))
            emit("probe.body_started", call_id=identifier, tool=tool.name)
            try:
                await asyncio.sleep(delay)
                fault = case.injected_failures.get(identifier)
                if fault == "exception":
                    raise ConnectionError("fixed simulated service outage")
                if fault == "error_prefix":
                    result = "错误: fixed injected write/service failure"
                else:
                    result = original(*args, **kwargs)
                    if inspect.isawaitable(result):
                        result = await result
                text = result if isinstance(result, str) else json.dumps(result)
                spans[identifier]["ok"] = not tool_failed(text)
                return result
            finally:
                spans[identifier]["ended"] = time.perf_counter()
                active.remove(identifier)
                emit("probe.body_finished", call_id=identifier, ok=spans[identifier]["ok"])

        tool.func = observed

    for name in {c.tool for c in case.calls}:
        instrument(bot.tool_registry.get(name))
    calls = [
        ToolCall(id=c.id, function={"name": c.tool, "arguments": json.dumps(c.arguments)})
        for c in case.calls
    ]

    async def execute_one(c):
        token = current_call.set(c.id)
        try:
            content = await bot.tool_registry.execute(c.function.name, c.function.arguments)
            return ToolResult(tool_call_id=c.id, name=c.function.name, content=content)
        finally:
            current_call.reset(token)

    trace = RunTrace(directory / "traces", capture_content=True)
    results, error_type = [], None
    started = time.perf_counter()
    try:
        with activate(trace), collect_deliveries() as delivered:
            results = await asyncio.wait_for(
                bot.tool_scheduler.execute(
                    calls, [ChatMessage(role="user", content=case.context)], execute_one
                ),
                timeout=60,
            )
    except Exception as exc:
        error_type = type(exc).__name__
        delivered = []
    elapsed = time.perf_counter() - started
    await bot.sandbox_router.stop()
    by_id = {r.tool_call_id: r.content for r in results}
    for child, parents in case.dependencies.items():
        if child not in spans:
            continue
        for parent in parents:
            p = spans.get(parent)
            if not p or p["ended"] is None or p["ended"] > spans[child]["started"]:
                violations.append(
                    {"kind": "dependency_started_early", "call": child, "parent": parent}
                )
            elif not p["ok"]:
                violations.append(
                    {"kind": "failed_parent_executed", "call": child, "parent": parent}
                )
    plan = next((e for e in trace.events if e["event"] == "scheduling.plan"), {})
    closure = ancestors(plan)
    relations = [(p, c) for c, ps in case.dependencies.items() for p in ps]
    covered = sum(p in closure.get(c, set()) for p, c in relations)
    overlap = []
    for group in case.independent_groups:
        overlap.append(
            any(
                min(spans[a]["ended"], spans[b]["ended"])
                > max(spans[a]["started"], spans[b]["started"])
                for i, a in enumerate(group)
                for b in group[i + 1 :]
                if a in spans and b in spans
            )
        )
    expected = {c.id for c in case.calls} - set(case.blocked + case.deferred)
    checks = {
        "expected_execution": set(spans) == expected,
        "expected_failures": {k for k, s in spans.items() if not s["ok"]} == set(case.failed),
        "required_order": not violations,
        "results": all(value in by_id.get(k, "") for k, value in case.result_contains.items()),
        "final_files": all(
            (not (workspace / path).exists())
            if value is None
            else (workspace / path).is_file()
            and (workspace / path).read_text(encoding="utf-8") == value
            for path, value in case.final_files.items()
        ),
        "blocked_or_deferred": all(
            "工具未执行" in by_id.get(k, "") for k in case.blocked + case.deferred
        ),
        "delivery": sorted(p.relative_to(workspace).as_posix() for p in delivered)
        == sorted(case.deliveries),
        "result_order": [r.tool_call_id for r in results] == [c.id for c in case.calls],
        "parallel_limit": peak <= 4,
    }
    model_calls = [e for e in trace.events if e["event"] == "model.finished"]
    row = {
        "case_id": case.id,
        "category": case.category,
        "variant": variant,
        "attempt": attempt,
        "execution_success": error_type is None and all(checks.values()),
        "checks": checks,
        "required_relations": len(relations),
        "covered_relations": covered,
        "dependency_graph_complete": covered == len(relations),
        "parallel_groups": len(overlap),
        "overlapped_groups": sum(overlap),
        "peak_parallel": peak,
        "duration_seconds": elapsed,
        "planning_seconds": sum(e["duration_seconds"] for e in model_calls),
        "metrics": trace.metrics(),
        "plan": plan,
        "violations": violations,
        "spans": spans,
        "results": by_id,
        "error_type": error_type,
        "fatal_provider_error": any(
            e.get("http_status") in {400, 401, 402, 403, 404, 405, 410, 422} for e in model_calls
        ),
    }
    row["success"] = row["execution_success"] and (
        not scheduled or row["dependency_graph_complete"]
    )
    atomic_json(directory / "result.json", row)
    return row


def summarize(rows):
    variants = {}
    for variant in sorted({r["variant"] for r in rows}):
        group = [r for r in rows if r["variant"] == variant]
        relations = sum(r["required_relations"] for r in group)
        opportunities = sum(r["parallel_groups"] for r in group)
        variants[variant] = {
            "attempts": len(group),
            "execution_passes": sum(r["execution_success"] for r in group),
            "contract_passes": sum(r["success"] for r in group),
            "required_relations": relations,
            "covered_relations": sum(r["covered_relations"] for r in group),
            "relation_coverage": sum(r["covered_relations"] for r in group) / relations
            if relations
            else None,
            "dependency_violations": sum(len(r["violations"]) for r in group),
            "parallel_groups": opportunities,
            "overlapped_groups": sum(r["overlapped_groups"] for r in group),
            "parallel_opportunity_rate": sum(r["overlapped_groups"] for r in group) / opportunities
            if opportunities
            else None,
            "peak_parallel": max(r["peak_parallel"] for r in group),
            "mean_seconds": statistics.mean(r["duration_seconds"] for r in group),
            "mean_planning_seconds": statistics.mean(r["planning_seconds"] for r in group),
            "mean_tokens": statistics.mean(r["metrics"]["reported_tokens"] for r in group),
            "usage_complete": all(r["metrics"]["usage_complete"] for r in group),
            "fallbacks": dict(Counter(str(r["plan"].get("fallback_reason")) for r in group)),
            "by_category": {
                category: {
                    "attempts": sum(r["category"] == category for r in group),
                    "execution_passes": sum(
                        r["execution_success"] for r in group if r["category"] == category
                    ),
                    "contract_passes": sum(
                        r["success"] for r in group if r["category"] == category
                    ),
                }
                for category in sorted({r["category"] for r in group})
            },
            "failures": [
                {
                    "case_id": r["case_id"],
                    "attempt": r["attempt"],
                    "failed_checks": [k for k, v in r["checks"].items() if not v],
                    "missing_relations": r["required_relations"] - r["covered_relations"],
                    "error_type": r["error_type"],
                }
                for r in group
                if not r["success"]
            ],
        }
    by_case = {}
    for name in sorted({r["case_id"] for r in rows}):
        by_case[name] = {}
        for variant in sorted({r["variant"] for r in rows if r["case_id"] == name}):
            group = [r for r in rows if r["case_id"] == name and r["variant"] == variant]
            by_case[name][variant] = {
                "attempts": len(group),
                "execution_passes": sum(r["execution_success"] for r in group),
                "contract_passes": sum(r["success"] for r in group),
                "mean_seconds": statistics.mean(r["duration_seconds"] for r in group),
            }
    return {"variants": variants, "by_case": by_case}


def write_report(root, rows):
    summary = summarize(rows)
    atomic_json(root / "summary.json", summary)
    lines = [
        "# 日常工具依赖测试",
        "",
        "真实文件工具；查询为固定模拟服务；规划为真实模型。没有主模型生成，不是完整任务成功率。",
        "执行通过要求实际顺序、返回内容、文件终态、失败阻断、延期及交付全部满足。",
        "DAG契约通过另外要求必要依赖在计划中可达；等价依赖图按传递闭包计分。",
        "串行按原始调用顺序执行，乱序与故障用例属于压力测试，不能用于推断日常失败率。",
        "",
        "| 场景 | 串行执行通过 | DAG执行通过 | DAG契约通过 | 串行平均秒 | DAG平均秒 |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for name, variants in summary["by_case"].items():
        a, b = variants.get("serial"), variants.get("dag")
        show = lambda value, key: f"{value[key]}/{value['attempts']}" if value else "—"  # noqa: E731
        lines.append(
            f"| {name} | {show(a, 'execution_passes')} | {show(b, 'execution_passes')} | "
            f"{show(b, 'contract_passes')} | "
            f"{a['mean_seconds']:.3f} | {b['mean_seconds']:.3f} |"
            if a and b
            else f"| {name} | {show(a, 'execution_passes')} | {show(b, 'execution_passes')} | "
            f"{show(b, 'contract_passes')} | — | — |"
        )
    (root / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


async def run(args):
    config = Config.from_yaml(args.config)
    selected = next(p for p in config.providers if p.name == config.default_provider)
    if not selected.api_key:
        raise ValueError("provider key missing")
    dataset = scenarios()
    root = Path(args.output).resolve() / uuid.uuid4().hex
    root.mkdir(parents=True)
    manifest = {
        "kind": "file_tools_and_simulated_queries_live_planner",
        "model": selected.model,
        "provider_name": selected.name,
        "auxiliary_thinking": selected.auxiliary_thinking,
        "repeats": args.repeats,
        "concurrency": 1,
        "max_parallel": 4,
        "instrumented_delay_seconds": args.delay,
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
            for index, case in enumerate(dataset):
                order = (False, True) if (attempt + index) % 2 else (True, False)
                for scheduled in order:
                    row = await run_case(
                        case, scheduled, attempt, root, provider, selected.model, delay=args.delay
                    )
                    rows.append(row)
                    atomic_json(root / "results.json", rows)
                    write_report(root, rows)
                    print(
                        f"{len(rows)}/{manifest['planned_attempts']} {case.id} "
                        f"{row['variant']} {attempt}: execute={row['execution_success']} "
                        f"contract={row['success']}",
                        flush=True,
                    )
                    if row["error_type"] or row["fatal_provider_error"]:
                        raise RuntimeError(f"infrastructure failure: {row['error_type']}")
    finally:
        await provider._client.close()
        manifest["completed_attempts"] = len(rows)
        manifest["run_status"] = (
            "completed" if len(rows) == manifest["planned_attempts"] else "interrupted"
        )
        atomic_json(root / "experiment.json", manifest)
    print(root / "report.md", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--output", default=".xclaw/evaluations/dependencies-20261030")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--delay", type=float, default=0.02)
    args = parser.parse_args()
    if args.repeats < 1 or not math.isfinite(args.delay) or args.delay < 0:
        parser.error("positive repeats and finite nonnegative delay required")
    logger.remove()
    logger.add(sys.stderr, level="CRITICAL")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
