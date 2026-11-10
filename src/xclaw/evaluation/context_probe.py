"""Separate measurements for compression size, retained input and semantic constraint recall."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
import time
import uuid
from pathlib import Path

from loguru import logger

from xclaw.ai.client import AIClient
from xclaw.ai.context import estimate_messages_tokens, protocol_boundaries
from xclaw.ai.provider import OpenAIProvider
from xclaw.ai.types import ChatMessage
from xclaw.evaluation.evidence import EvidencePacket
from xclaw.evaluation.judge import RUBRIC_HASH, evaluate
from xclaw.persistence import atomic_json
from xclaw.server.config import Config
from xclaw.telemetry import RunTrace, activate

CASES = {
    "money": [
        "金额计算必须使用 Decimal，禁止 float。",
        "输入和输出均为字符串。",
        "结果保留两位小数。",
    ],
    "public_api": [
        "不得改变公开函数名和参数顺序。",
        "不得引入新的第三方依赖。",
        "实现必须兼容 Python 3.11。",
    ],
    "user_lookup": ["用户 ID 区分大小写。", "查找不到用户时返回 None。", "不得修改输入字典。"],
}


async def run_probe(
    case_id, requirements, strategy, provider, model, directory, *, with_judge=False
):
    directory.mkdir(parents=True, exist_ok=False)
    history = [
        ChatMessage(role="user", content="项目约束：\n" + "\n".join(requirements)),
        ChatMessage(role="assistant", content="已记录这些要求。"),
    ]
    for index in range(4):
        history.extend(
            [
                ChatMessage(
                    role="user",
                    content=f"无关讨论{index}："
                    + "以下是目录命名和开发安排讨论，没有新增要求。" * 35,
                ),
                ChatMessage(role="assistant", content="讨论已记录，不改变原项目约束。" * 8),
            ]
        )
    current = [ChatMessage(role="user", content="继续工作，遵守此前项目约束。")]
    client = AIClient(provider, system_prompt="你是编程助手。", model=model, max_tokens=256)
    client.context_strategy = strategy
    client.enable_compression(context_window=2048, safety_margin=128, keep_recent=2)
    client.set_history(history)
    before = client.compressor.estimate_request_tokens(
        history, current, system_prompt=client.system_prompt
    )
    trace = RunTrace(str(directory / "traces"))
    started = time.perf_counter()
    row = {
        "case_id": case_id,
        "strategy": strategy,
        "status": "error",
        "before_estimated_tokens": before,
    }
    packet = None
    with activate(trace):
        try:
            async with asyncio.timeout(180):
                prepared = await client._prepare_messages(current, None)
            protocol_boundaries(prepared)
            after = estimate_messages_tokens(prepared)
            input_preserved = (
                prepared[-1].role == "user" and prepared[-1].content == current[0].content
            )
            row.update(
                status="prepared",
                after_estimated_tokens=after,
                estimated_reduction_ratio=1 - after / before,
                fits_estimated_budget=after <= client.compressor.input_budget,
                current_user_input_preserved=input_preserved,
                protocol_valid=True,
            )
            atomic_json(directory / "prepared_messages.json", [m.model_dump() for m in prepared])
            retained = "\n".join(m.content or "" for m in client.get_history())
            sources = {
                "requirements": json.dumps(
                    {f"r{i + 1}": r for i, r in enumerate(requirements)}, ensure_ascii=False
                ),
                "answer": retained or "[retained history is empty]",
            }
            task_hash = hashlib.sha256(
                json.dumps(requirements, ensure_ascii=False).encode()
            ).hexdigest()
            packet = EvidencePacket(
                "context",
                sources,
                [f"r{i + 1}" for i in range(len(requirements))],
                {},
                row.copy(),
                [],
                {
                    "case_id": case_id + "-" + strategy,
                    "task_id": case_id,
                    "variant": strategy,
                    "attempt": 1,
                    "task_hash": task_hash,
                    "generator_model": model,
                },
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            row.update(
                status="preparation_error",
                error_type=type(exc).__name__,
                http_status=getattr(exc, "status_code", None),
            )
    row.update(duration_seconds=time.perf_counter() - started, metrics=trace.metrics())
    if packet and with_judge:
        judged = await evaluate(
            packet, provider, model=model, trace_dir=str(directory / "judge-traces")
        )
        row["semantic_judgment"] = judged
        row["semantic_constraint_retention_ratio"] = judged.get("requirement_satisfaction_ratio")
        row["semantic_constraint_coverage"] = judged.get("requirement_score_coverage")
    atomic_json(directory / "result.json", row)
    return row


async def main_async(args):
    config = Config.from_yaml(args.config)
    name = args.provider or config.default_provider
    pc = next((p for p in config.providers if p.name == name), None)
    if not pc or not pc.api_key or pc.context_window < 2048:
        raise ValueError("Provider missing or configured context window is too small")
    provider = OpenAIProvider(pc.api_key, pc.base_url, auxiliary_thinking=False)
    root = Path(args.output or Path(".xclaw/quality/context") / uuid.uuid4().hex).resolve()
    manifest = {
        "kind": "context_component_probe",
        "model": pc.model,
        "cases": {case_id: CASES[case_id] for case_id in args.cases},
        "strategies": args.strategies,
        "context_window": 2048,
        "max_output_tokens": 256,
        "safety_margin": 128,
        "keep_recent": 2,
        "judge_enabled": args.judge,
        "rubric_hash": RUBRIC_HASH,
        "implementation_hash": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "run_status": "running",
    }
    atomic_json(root / "manifest.json", manifest)
    rows = []
    try:
        for case_id in args.cases:
            for strategy in args.strategies:
                row = await run_probe(
                    case_id,
                    CASES[case_id],
                    strategy,
                    provider,
                    pc.model,
                    root / (case_id + "-" + strategy),
                    with_judge=args.judge,
                )
                rows.append(row)
                atomic_json(root / "results.json", rows)
                print(case_id, strategy, row["status"], flush=True)
                if (
                    row.get("http_status") in {400, 401, 402, 403, 404, 422}
                    or row.get("semantic_judgment", {}).get("status") == "provider_error"
                ):
                    manifest["run_status"] = "blocked_provider"
                    break
            else:
                continue
            break
        if manifest["run_status"] == "running":
            manifest["run_status"] = "completed"
    except BaseException:
        manifest["run_status"] = "interrupted"
        raise
    finally:
        await provider._client.close()
        manifest["finished_cases"] = len(rows)
        atomic_json(root / "manifest.json", manifest)
    lines = [
        "# 上下文组件评测",
        "",
        "估算长度缩减不等于真实 Token 节省；语义保留率来自独立 Judge，尚需人工校准。",
        "",
        "| 案例 | 策略 | 准备状态 | 估算缩减 | 当前输入保留 | 语义约束保留 |",
        "|---|---|---|---:|---|---:|",
    ]
    for row in rows:
        reduction = (
            f"{row['estimated_reduction_ratio']:.1%}"
            if "estimated_reduction_ratio" in row
            else "不可测"
        )
        retention = (
            f"{row['semantic_constraint_retention_ratio']:.1%}"
            if row.get("semantic_constraint_retention_ratio") is not None
            else "未评分"
        )
        lines.append(
            f"| {row['case_id']} | {row['strategy']} | {row['status']} | {reduction} | "
            f"{row.get('current_user_input_preserved')} | {retention} |"
        )
    (root / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(root / "report.md")


def main():
    parser = argparse.ArgumentParser(description="单独评测上下文压缩，不运行编程任务")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--provider")
    parser.add_argument("--output")
    parser.add_argument("--cases", nargs="+", choices=list(CASES), default=list(CASES))
    parser.add_argument(
        "--strategies", nargs="+", choices=["summary", "truncate"], default=["summary", "truncate"]
    )
    parser.add_argument("--judge", action="store_true", help="额外调用 Judge 评价语义约束保留")
    args = parser.parse_args()
    if len(set(args.cases)) != len(args.cases) or len(set(args.strategies)) != len(args.strategies):
        parser.error("Cases and strategies must not be duplicated")
    logger.remove()
    logger.add(sys.stderr, level="WARNING")
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
