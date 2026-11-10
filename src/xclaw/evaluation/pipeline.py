"""Run the Agent, accept its task independently, then grade evidence with an isolated judge."""

from pathlib import Path

from xclaw.evaluation.evidence import build_case_packet
from xclaw.evaluation.judge import evaluate
from xclaw.evaluation.runner import run_case, validate_docker
from xclaw.persistence import atomic_json


async def run_quality_case(
    task,
    variant,
    attempt,
    root,
    settings,
    agent_provider,
    judge_provider,
    *,
    judge_model,
    validator=validate_docker,
):
    """Judge evidence only; no tool registry or execution permission is passed to the judge."""
    root = Path(root).resolve()
    objective = await run_case(
        task, variant, attempt, root, settings, agent_provider, validator=validator
    )
    if objective["status"] in {"provider_balance_unavailable", "provider_configuration_error"}:
        return {"objective": objective, "quality": {"status": "skipped_provider_unavailable"}}
    packet = build_case_packet(objective, root, {task.id: task})
    case = Path(objective["artifact_directory"])
    quality = await evaluate(
        packet, judge_provider, model=judge_model, trace_dir=str(case / "judge-traces")
    )
    result = {"objective": objective, "quality": quality, "components": packet.components}
    atomic_json(case / "quality.json", result)
    return result
