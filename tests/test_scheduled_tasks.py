"""Exercise restart boundaries and the configured page-to-email path."""

import asyncio
import json
import smtplib
import subprocess
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import httpx
import pytest

from xclaw.server.schedule_config import ScheduledJob, SchedulerConfig
from xclaw.server.scheduler import Scheduler


def test_daily_time_uses_the_configured_offset():
    job = ScheduledJob(id="brief", prompt="brief", daily_at="09:00", utc_offset="+08:00")
    assert job.next_after(datetime(2026, 11, 16, 0, 59, tzinfo=timezone.utc)) == datetime(
        2026, 11, 16, 1, tzinfo=timezone.utc,
    )
    assert job.next_after(datetime(2026, 11, 16, 1, tzinfo=timezone.utc)) == datetime(
        2026, 11, 17, 1, tzinfo=timezone.utc,
    )


async def test_github_job_compares_with_last_successful_check_after_restart(tmp_path, monkeypatch):
    repo = "liuchang1937-byte/xclaw"
    remote = f"https://github.com/{repo}"
    first, second = "a" * 40, "b" * 40
    replies = iter([(0, first), (0, first), (128, ""), (0, second)])

    def git(args, **kwargs):
        assert args == [
            "git", "-c", "credential.helper=", "ls-remote", "--exit-code", remote, "HEAD",
        ]
        assert kwargs["env"]["GIT_TERMINAL_PROMPT"] == "0"
        code, sha = next(replies)
        return subprocess.CompletedProcess(args, code, f"{sha}\tHEAD\n")

    monkeypatch.setattr(subprocess, "run", git)
    config = SchedulerConfig(state_dir=str(tmp_path), jobs=[{
        "id": "check", "daily_at": "12:00", "utc_offset": "+08:00", "github_repo": repo,
    }])
    path = tmp_path / "job-check.json"
    state = {"cadence": config.jobs[0].cadence}
    run_agent = Mock(side_effect=AssertionError("repository checks should not call the model"))
    for expected_status, expected_result, expected_sha in [
        ("completed", "首次检查", first),
        ("completed", "无更新", first),
        ("failed", None, first),
        ("completed", "有更新", second),
    ]:
        due = datetime.now(timezone.utc) - timedelta(seconds=1)
        state["next_run_at"] = due.isoformat()
        path.write_text(json.dumps(state), encoding="utf-8")
        scheduler = Scheduler(config, run_agent)
        scheduler.start()
        try:
            async with asyncio.timeout(4):
                while True:
                    state = json.loads(path.read_bytes())
                    record = state["last_run"]
                    if (
                        record and record["finished_at"]
                        and datetime.fromisoformat(record["scheduled_at"]) == due
                    ):
                        break
                    await asyncio.sleep(0.01)
        finally:
            await scheduler.stop()
        assert state["github_head"] == f"{remote}/commit/{expected_sha}"
        assert record["status"] == expected_status
        if expected_result is None:
            assert "Git 退出码 128" in record["error"]
        else:
            assert expected_result in record["result"]
    assert f"{remote}/commit/{first}" in record["result"]
    run_agent.assert_not_called()


@pytest.mark.parametrize("scenario", ["success", "broken_source", "lost_smtp_ack"])
async def test_overdue_job_reads_website_and_delivers_by_configuration(
    tmp_path, monkeypatch, scenario,
):
    config = SchedulerConfig(
        state_dir=str(tmp_path),
        smtp={"host": "smtp.example.com", "sender": "reports@example.com"},
        jobs=[{
            "id": "brief", "prompt": "整理网站更新", "every_seconds": 60,
            "urls": ["https://example.com/news"],
            "email": {"to": ["reader@example.com"], "subject": "更新简报"},
        }],
    )
    due = datetime.now(timezone.utc) - timedelta(hours=1)
    path = tmp_path / "job-brief.json"
    path.write_text(json.dumps({
        "next_run_at": due.isoformat(), "cadence": "every:60",
    }), encoding="utf-8")
    requested = []

    def web_response(request):
        requested.append(str(request.url))
        if scenario == "broken_source":
            return httpx.Response(503)
        return httpx.Response(
            200, headers={"Content-Type": "text/html; charset=utf-8"},
            content='<p>版本已发布</p><script>hidden content</script>'.encode(),
        )

    client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: client(
        **kw, transport=httpx.MockTransport(web_response),
    ))
    agent_calls = []

    async def run_agent(job, prompt, scheduled_at):
        agent_calls.append(job.id)
        assert scheduled_at == due
        assert "版本已发布" in prompt and "https://example.com/news" in prompt
        assert "hidden content" not in prompt
        return "简报正文"

    def deliver(message, from_addr, to_addrs):
        before = json.loads(path.read_bytes())["last_run"]
        assert before["status"] == "sending" and before["result"] == "简报正文"
        assert to_addrs == ["reader@example.com"] and from_addr == "reports@example.com"
        assert message["Subject"] == "更新简报" and message.get_content().strip() == "简报正文"
        if scenario == "lost_smtp_ack":
            raise smtplib.SMTPServerDisconnected("server disconnected after DATA")
        return {}

    smtp_client = Mock()
    smtp_client.send_message.side_effect = deliver
    smtp_ssl = Mock(return_value=smtp_client)
    monkeypatch.setattr(smtplib, "SMTP_SSL", smtp_ssl)
    scheduler = Scheduler(config, run_agent)
    scheduler.start()
    try:
        async with asyncio.timeout(4):
            while True:
                record = json.loads(path.read_bytes())["last_run"]
                if record and record["finished_at"]:
                    break
                await asyncio.sleep(0.01)
    finally:
        await scheduler.stop()
    assert requested == ["https://example.com/news"]
    if scenario == "broken_source":
        assert agent_calls == []
        smtp_ssl.assert_not_called()
        assert record["status"] == "failed" and "503" in record["error"]
    else:
        assert agent_calls == ["brief"]
        smtp_client.send_message.assert_called_once()
        assert record["status"] == ("completed" if scenario == "success" else "delivery_unknown")
        assert record["result"] == "简报正文"
    assert datetime.fromisoformat(json.loads(path.read_bytes())["next_run_at"]) > due


async def test_restart_marks_interrupted_delivery_without_replaying(tmp_path):
    config = SchedulerConfig(state_dir=str(tmp_path), jobs=[{
        "id": "brief", "prompt": "整理网站更新", "every_seconds": 3600,
    }])
    path = tmp_path / "job-brief.json"
    original = datetime.now(timezone.utc)
    path.write_text(json.dumps({
        "cadence": "every:3600", "next_run_at": (original + timedelta(hours=1)).isoformat(),
        "last_run": {
            "scheduled_at": original.isoformat(), "started_at": original.isoformat(),
            "status": "sending", "result": "已生成",
        },
    }), encoding="utf-8")
    run_agent = Mock(side_effect=AssertionError("a previously claimed run must not be replayed"))
    scheduler = Scheduler(config, run_agent)
    scheduler.start()
    try:
        with pytest.raises(RuntimeError, match="另一个进程"):
            Scheduler(config, run_agent).start()
        await asyncio.sleep(0)
    finally:
        await scheduler.stop()
    run_agent.assert_not_called()
    record = json.loads(path.read_bytes())["last_run"]
    assert record["status"] == "delivery_unknown" and record["result"] == "已生成"
