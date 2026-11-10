import json
import subprocess
import sys

import pytest

from xclaw.ai.provider import BaseProvider
from xclaw.ai.types import ChatResponse, StreamChunk, ToolCall
from xclaw.evaluation.runner import EvalSettings, run_case, self_check, summarize, write_report
from xclaw.evaluation.tasks import builtin_tasks, load_tasks


def test_task_oracles_reject_seeds_and_accept_all_references(tmp_path):
    report = self_check(tmp_path, builtin_tasks())
    assert report["passed"]
    assert len(report["tasks"]) == 24
    assert report["model_called"] is False


class FixProvider(BaseProvider):
    def __init__(self, task, *, fix=True):
        self.task, self.fix, self.turn = task, fix, 0

    async def chat(self, messages, tools=None, **kwargs):
        self.turn += 1
        if self.turn == 1 and self.fix:
            return ChatResponse(
                tool_calls=[
                    ToolCall(
                        id="write",
                        function={
                            "name": "write_file",
                            "arguments": json.dumps(
                                {
                                    "path": "app.py",
                                    "content": self.task.reference["app.py"],
                                }
                            ),
                        },
                    )
                ]
            )
        return ChatResponse(content="finished")

    async def chat_stream(self, *args, **kwargs):
        yield StreamChunk(delta_content="finished")


async def trusted_fixture_validation(task, workspace, oracle, settings):
    # The test provider emits our reference; never use this host validator for a live model.
    result = subprocess.run(
        [sys.executable, "-I", str(oracle / "validate.py"), str(workspace.work)],
        capture_output=True,
        timeout=10,
    )
    return {"passed": result.returncode == 0, "infrastructure_error": False}


@pytest.mark.parametrize("fix", [True, False])
async def test_real_agent_loop_is_accepted_by_objective_checks_not_its_reply(tmp_path, fix):
    task = builtin_tasks()[0]
    row = await run_case(
        task,
        "summary_serial",
        1,
        tmp_path,
        EvalSettings(model="fake"),
        FixProvider(task, fix=fix),
        validator=trusted_fixture_validation,
    )
    assert row["success"] is fix
    assert row["status"] == ("completed" if fix else "acceptance_failed")
    assert row["metrics"]["usage_complete"] is False
    seed = tmp_path / "clamp-summary_serial-1" / "seed" / "app.py"
    assert seed.read_text() == task.seed["app.py"]
    write_report(tmp_path, [row])
    assert json.loads((tmp_path / "summary.json").read_text())["summary_serial"]["attempts"] == 1


def test_summary_keeps_failures_in_denominator_and_small_sample_p95_unknown():
    rows = [
        {
            "variant": "a",
            "task_id": "x",
            "category": "bug",
            "success": ok,
            "status": "completed" if ok else "timeout",
            "duration_seconds": duration,
            "metrics": {"usage_complete": ok, "reported_tokens": 12},
        }
        for ok, duration in [(True, 1), (False, 3)]
    ]
    summary = summarize(rows)["a"]
    assert summary["success_rate"] == 0.5
    assert summary["failures"] == {"timeout": 1}
    assert summary["median_seconds"] == 2
    assert summary["p95_seconds"] is None
    assert summary["usage_complete"] is False


def test_custom_repository_suite_keeps_validator_outside_fixture(tmp_path):
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    (fixture / "app.py").write_text("def run(): return 1\n")
    (fixture / ".env").write_text("excluded")
    (tmp_path / "validator.py").write_text("print('XCLAW_VALIDATION_OK:1')\n")
    entry = {
        "id": "real_bug",
        "category": "repository",
        "prompt": "fix",
        "fixture": "fixture",
        "validator": "validator.py",
        "allowed_write_paths": ["*.py"],
    }
    manifest = tmp_path / "suite.json"
    manifest.write_text(json.dumps({"tasks": [entry]}))
    task = load_tasks(manifest)[0]
    assert task.seed == {"app.py": "def run(): return 1\n"}
    assert task.allowed_write_paths == ["*.py"]
    entry["validator"] = "fixture/app.py"
    manifest.write_text(json.dumps({"tasks": [entry]}))
    with pytest.raises(ValueError, match="不可见|不能位于"):
        load_tasks(manifest)
