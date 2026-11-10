"""Bound and blind grading evidence. Never execute code or expose reference implementations."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

from xclaw.evaluation.quality_metrics import trajectory_metrics
from xclaw.evaluation.tasks import Task, builtin_tasks


@dataclass
class EvidencePacket:
    subject: str
    sources: dict[str, str]
    requirement_ids: list[str]
    objective: dict
    components: dict
    limited_sources: list[str]
    metadata: dict

    def payload(self) -> dict:
        # No variant labels, aggregate results or generating model identity reach the judge.
        return {
            "subject": self.subject,
            "sources": self.sources,
            "requirement_ids": self.requirement_ids,
            "limited_sources": self.limited_sources,
        }

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(
            json.dumps(self.payload(), sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()


def bounded(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    marker = "\n[evidence truncated]\n"
    keep = max(0, limit - len(marker))
    return text[: keep // 2] + marker + (text[-(keep - keep // 2) :] if keep else ""), True


def safe_text(path: Path, root: Path, limit=8000) -> tuple[str, bool]:
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("Evidence path escapes its artifact directory")
    if not path.is_file():
        return "[artifact missing]", True
    with path.open("r", encoding="utf-8", errors="replace") as stream:
        content = stream.read(limit + 1)
    if len(content) <= limit:
        return content, False
    return content[: limit - 24] + "\n[evidence truncated]\n", True


def read_json(path: Path, root: Path):
    text, limited = safe_text(path, root, 2_000_000)
    if limited:
        raise ValueError("Missing or oversized artifact JSON")
    return json.loads(text)


def case_directory(row: dict, root: Path) -> Path:
    if not all(re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", row[k]) for k in ("task_id", "variant")):
        raise ValueError("Invalid task or variant ID")
    if type(row["attempt"]) is not int or row["attempt"] < 1:
        raise ValueError("Invalid attempt number")
    case = root / f"{row['task_id']}-{row['variant']}-{row['attempt']}"
    if not case.resolve().is_relative_to(root.resolve()):
        raise ValueError("Case path escapes experiment")
    if not case.is_dir():
        raise ValueError("Case directory missing")
    return case


def read_events(case: Path) -> list[dict]:
    events = []
    for path in sorted((case / "traces").glob("*.jsonl")):
        text, limited = safe_text(path, case, 2_000_000)
        if limited:
            raise ValueError("Oversized trace")
        for line in text.splitlines():
            events.append(json.loads(line))
    return events


def task_metadata(row: dict, case: Path, tasks: dict[str, Task] | None = None) -> dict:
    if (case / "task.json").exists():
        data = read_json(case / "task.json", case)
        if data["task_hash"] != row["task_hash"]:
            raise ValueError("Task evidence baseline hash mismatch")
        return data
    tasks = tasks if tasks is not None else {task.id: task for task in builtin_tasks()}
    task = tasks.get(row["task_id"])
    if task is None or task.fingerprint != row["task_hash"]:
        raise ValueError("Legacy task evidence unavailable; supply the original custom suite")
    return {
        "id": task.id,
        "task_hash": task.fingerprint,
        "prompt": task.prompt,
        "history": task.history,
        "evaluation_requirements": task.evaluation_requirements,
        "expected_tool_groups": task.expected_tool_groups,
    }


def build_case_packet(row: dict, root: Path, tasks=None) -> EvidencePacket:
    case = case_directory(row, root)
    task = task_metadata(row, case, tasks)
    requirements = task.get("evaluation_requirements") or [
        m["content"] for m in task.get("history", []) if m["role"] == "user"
    ] + [task["prompt"]]
    refs = {f"r{i + 1}": text for i, text in enumerate(requirements)}
    sources = {"requirements": json.dumps(refs, ensure_ascii=False), "task": task["prompt"]}
    limited = []

    def add(name, text, is_limited=False):
        sources[name] = text
        if is_limited:
            limited.append(name)

    answer, clipped = safe_text(case / "response.txt", case, 4000)
    add("answer", answer if answer else "[recorded answer is empty]", clipped)
    validation = (
        read_json(case / "validation.json", case) if (case / "validation.json").exists() else None
    )
    execution = {
        "status": row["status"],
        "tests_passed": row.get("validation_passed"),
        "path_constraints_passed": row.get("constraints_passed"),
        "validation": (
            {k: v for k, v in validation.items() if k not in {"stdout", "stderr"}}
            if validation is not None
            else None
        ),
    }
    # Preserve literal log lines. Embedding logs in serialized JSON would turn real
    # newlines into backslash-n and reject otherwise faithful multiline citations.
    execution_text = json.dumps(execution, ensure_ascii=False)
    if validation is not None:
        for stream in ("stdout", "stderr"):
            execution_text += f"\n--- validator {stream} ---\n" + validation.get(stream, "")
    text, clipped = bounded(execution_text, 4000)
    add("execution", text, clipped)
    snapshots = list((case / "tasks").glob("*/manifest.json"))
    if len(snapshots) != 1:
        raise ValueError("Expected exactly one task snapshot")
    snapshot = snapshots[0].parent
    diff, clipped = safe_text(snapshot / "changes.diff", case, 4000)
    add("diff", diff if diff else "[recorded diff is empty]", clipped)
    baseline = read_json(snapshot / "manifest.json", case)
    paths = sorted(set(baseline["files"]) | {c["path"] for c in row.get("changes", [])})
    budget = 8000
    for relative in paths:
        if relative == "README.md":
            continue
        path = snapshot / "work" / relative
        if budget < 200:
            limited.append("remaining_code")
            break
        content, clipped = safe_text(path, snapshot / "work", min(3000, budget))
        add("code:" + relative, content, clipped)
        budget -= len(content)
    events = read_events(case)
    components = trajectory_metrics(events, task.get("expected_tool_groups", []))
    observed = [
        {k: e[k] for k in ("event", "tool", "status") if k in e}
        for e in events
        if e["event"] in {"tool.finished", "tool.skipped"}
    ]
    text, clipped = bounded(json.dumps(observed, ensure_ascii=False), 2000)
    add("tool_trajectory", text, clipped)
    # Bound requirements too, but mark it; never silently claim complete evaluation evidence.
    sources["requirements"], clipped = bounded(sources["requirements"], 6000)
    if clipped:
        limited.append("requirements")
    return EvidencePacket(
        "coding_task",
        sources,
        list(refs),
        {
            "success": row["success"],
            "status": row["status"],
            "validation_passed": row.get("validation_passed"),
            "constraints_passed": row.get("constraints_passed"),
        },
        components,
        limited,
        {
            "case_id": case.name,
            "variant": row["variant"],
            "category": row["category"],
            "task_id": row["task_id"],
            "attempt": row["attempt"],
            "task_hash": row["task_hash"],
            "evaluation_hash": row.get("evaluation_hash"),
            "generator_model": row.get("settings", {}).get("model"),
        },
    )
