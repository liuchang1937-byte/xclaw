"""AgentBench FC / AgentRL HTTP adapter using XClaw's model and context client.

The benchmark owns action execution and scoring. Local CodingAgent tools and its
dependency scheduler are deliberately outside this protocol's execution boundary.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import subprocess
import time
import uuid
from pathlib import Path

import httpx

from xclaw.ai.client import AIClient
from xclaw.ai.context import ContextBudgetExceededError
from xclaw.ai.provider import OpenAIProvider
from xclaw.ai.types import ChatMessage, ToolDefinition
from xclaw.evaluation.runner import dependency_versions
from xclaw.persistence import atomic_json
from xclaw.server.config import Config
from xclaw.telemetry import RunTrace, activate, capture, model_purpose

AGENTBENCH_REVISION = "d1e4a10db08c87075c78972e48ecc182be03e2d5"
AGENTRL_REVISION = "6a73409d31ba695d383b978a8ad3ef400d90c054"


class Controller:
    def __init__(self, url, *, transport=None):
        self.client = httpx.AsyncClient(
            base_url=url.rstrip("/") + "/",
            timeout=httpx.Timeout(120, connect=5),
            transport=transport,
            trust_env=False,
        )

    async def request(self, method, path, **kwargs):
        # Do not retry state-changing operations: an ambiguous timeout can mean
        # the environment already executed the action.
        response = await self.client.request(method, path, **kwargs)
        response.raise_for_status()
        return response

    async def indices(self, task):
        result = (await self.request("GET", "get_indices", params={"name": task})).json()
        if not isinstance(result, list):
            raise ValueError("invalid task indices")
        return result

    async def start(self, task, index):
        response = await self.request(
            "POST",
            "start_sample",
            json={
                "name": task,
                "index": index,
                "custom_task": None,
            },
        )
        return int(response.headers["session_id"]), response.json()

    async def interact(self, session, message):
        return (
            await self.request(
                "POST",
                "interact",
                headers={"session_id": str(session)},
                json={"messages": [message]},
            )
        ).json()

    async def cancel(self, session):
        await self.request("POST", "cancel", headers={"session_id": str(session)})


def messages_from_environment(history, incoming):
    """Official FC semantics: system starts a full history; otherwise append deltas."""
    if not incoming:
        raise ValueError("running environment response has no messages")
    for raw in incoming:
        raw = dict(raw)
        if raw.get("role") == "developer":
            raw["role"] = "system"
        if raw.get("role") == "system":
            history = []
        content = raw.get("content")
        if isinstance(content, list):
            if any(part.get("type") != "text" for part in content):
                raise ValueError("this adapter supports text environments only")
            raw["content"] = "".join(part["text"] for part in content)
        history.append(ChatMessage.model_validate(raw))
    return history


async def run_sample(controller, client, task, index, directory, *, max_turns=32, timeout=300):
    directory.mkdir(parents=True, exist_ok=False)
    trace = RunTrace(directory / "traces", capture_content=True)
    row = {
        "task": task,
        "index": index,
        "status": "adapter_error",
        "success": None,
        "reward": None,
        "official_result": None,
        "turns": 0,
        "session_id": None,
        "external_tool_call_proposals": 0,
    }
    session, finished = None, False
    started = time.perf_counter()
    try:
        with activate(trace), model_purpose("main"):
            async with asyncio.timeout(timeout):
                session, state = await controller.start(task, index)
                row["session_id"] = session
                history, tools = [], None
                while True:
                    capture("agentbench.environment", response=state)
                    if state.get("finish") is True:
                        finished = True
                        row["official_result"] = state
                        row["status"] = state.get("status") or "unknown"
                        row["reward"] = state.get("reward")
                        # COMPLETED is not proof of success. Mirror AgentRL's
                        # success_rate metric: reward == 1, missing reward unknown.
                        valid = row["status"] in {
                            "unknown",
                            "completed",
                            "agent validation failed",
                            "agent invalid action",
                            "task limit reached",
                        }
                        if valid and row["reward"] is not None:
                            row["success"] = row["reward"] == 1.0
                        break
                    if not state.get("messages"):
                        # Some controller versions omit terminal error fields in
                        # start_sample. An empty observation is an environment
                        # protocol failure, never a failed model answer.
                        row["status"] = "environment_empty_observation"
                        row["last_environment_response"] = state
                        break
                    if row["turns"] >= max_turns:
                        row["status"] = "client_turn_limit"
                        break
                    history = messages_from_environment(history, state.get("messages"))
                    if state.get("tools"):
                        tools = []
                        for tool in state["tools"]:
                            if tool.get("type") != "function":
                                raise ValueError("unsupported benchmark tool type")
                            tools.append(ToolDefinition.model_validate(tool["function"]))
                    response = await client.chat(history, tools=tools, save_history=False)
                    row["turns"] += 1
                    row["external_tool_call_proposals"] += len(response.tool_calls)
                    if response.finish_reason == "length":
                        row["status"] = "model_output_truncated"
                        break
                    if not response.content and not response.tool_calls:
                        row["status"] = "empty_model_response"
                        break
                    action = ChatMessage(
                        role="assistant",
                        content=response.content,
                        tool_calls=response.tool_calls or None,
                        reasoning_content=response.reasoning_content,
                    )
                    history.append(action)
                    state = await controller.interact(session, action.to_openai_format())
    except TimeoutError:
        row["status"] = "timeout"
    except ContextBudgetExceededError:
        row["status"] = "context_budget"
    except Exception as exc:
        row["error_type"] = type(exc).__name__
    finally:
        if session is not None and not finished:
            try:
                await controller.cancel(session)
                row["cancelled"] = True
            except Exception as exc:
                row["cleanup_error_type"] = type(exc).__name__
        row["duration_seconds"] = time.perf_counter() - started
        row["metrics"] = trace.metrics()
        atomic_json(directory / "result.json", row)
    return row


def summarize(rows):
    known = [r for r in rows if r["success"] is not None]
    successes = sum(r["success"] is True for r in rows)
    return {
        "attempts": len(rows),
        "successes": successes,
        "judged_attempts": len(known),
        "unknown_or_errors": len(rows) - len(known),
        "success_rate_all_attempts": successes / len(rows) if rows else None,
        "judged_success_rate": successes / len(known) if known else None,
        "complete_official_judgments": len(known) == len(rows),
    }


def revision(path):
    path = path.resolve()
    result = subprocess.run(
        ["git", "-c", f"safe.directory={path.as_posix()}", "-C", str(path), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


async def run(args):
    controller = Controller(args.controller)
    provider = None
    try:
        available = await controller.indices(args.task)
        if args.check:
            print(
                json.dumps(
                    {"task": args.task, "count": len(available), "first_indices": available[:5]}
                )
            )
            return
        if revision(args.benchmark_root) != AGENTBENCH_REVISION:
            raise ValueError("AgentBench checkout differs from the verified revision")
        indices = available[: args.limit]
        if args.indices:
            indices = [json.loads(i) if i.isdecimal() else i for i in args.indices]
            if any(i not in available for i in indices):
                raise ValueError("requested index is not in the official task indices")
            if len(indices) != len(set(indices)):
                raise ValueError("duplicate requested indices")
        if not indices:
            raise ValueError("no official samples selected")
        config = Config.from_yaml(args.config)
        selected = next(p for p in config.providers if p.name == config.default_provider)
        if not selected.api_key:
            raise ValueError("provider key missing")
        provider = OpenAIProvider(
            selected.api_key, selected.base_url, auxiliary_thinking=selected.auxiliary_thinking
        )
        root = args.output.resolve() / uuid.uuid4().hex
        root.mkdir(parents=True)
        manifest = {
            "suite": "AgentBench-FC",
            "kind": "benchmark",
            "split": args.split,
            "agentbench_revision": AGENTBENCH_REVISION,
            "protocol_reference": AGENTRL_REVISION,
            "adapter_mode": "xclaw_client_external_execution",
            "dependency_scheduler_evaluated": False,
            "model": selected.model,
            "context_strategy": "summary",
            "context_window": args.context_window,
            "max_tokens": args.max_tokens,
            "max_turns": args.max_turns,
            "timeout": args.timeout,
            "temperature": 0,
            "task": args.task,
            "indices": indices,
            "repeats": args.repeats,
            "concurrency": 1,
            "dependencies": dependency_versions(),
            "adapter_hash": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "environment_version_note": args.environment_version,
            "environment_verified": False,
            "run_status": "running",
        }
        atomic_json(root / "experiment.json", manifest)
        rows = []
        try:
            for repeat in range(1, args.repeats + 1):
                for ordinal, index in enumerate(indices):
                    client = AIClient(
                        provider, model=selected.model, temperature=0, max_tokens=args.max_tokens
                    )
                    client.enable_compression(context_window=args.context_window)
                    row = await run_sample(
                        controller,
                        client,
                        args.task,
                        index,
                        root / f"sample-{ordinal}-{repeat}",
                        max_turns=args.max_turns,
                        timeout=args.timeout,
                    )
                    row["repeat"] = repeat
                    rows.append(row)
                    atomic_json(root / "results.json", rows)
                    atomic_json(root / "summary.json", summarize(rows))
                    print(
                        f"{index} repeat={repeat}: {row['status']} reward={row['reward']}",
                        flush=True,
                    )
        finally:
            manifest["run_status"] = (
                "completed" if len(rows) == len(indices) * args.repeats else "interrupted"
            )
            atomic_json(root / "experiment.json", manifest)
        print(root)
    finally:
        await controller.client.aclose()
        if provider is not None:
            await provider._client.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--controller", default="http://127.0.0.1:5020/api/")
    parser.add_argument("--task", default="os-std")
    parser.add_argument(
        "--check", action="store_true", help="list official indices without model calls"
    )
    parser.add_argument("--benchmark-root", type=Path, default=Path(".xclaw/benchmarks/AgentBench"))
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--output", type=Path, default=Path(".xclaw/evaluations/agentbench-fc"))
    parser.add_argument("--indices", nargs="+")
    parser.add_argument("--limit", type=int, default=3)
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--max-turns", type=int, default=32)
    parser.add_argument("--max-tokens", type=int, default=4096)
    parser.add_argument("--context-window", type=int, default=32768)
    parser.add_argument("--timeout", type=float, default=300)
    parser.add_argument("--split", choices=["dev", "test", "unspecified"], default="unspecified")
    parser.add_argument(
        "--environment-version",
        required=False,
        help="record deployed controller/worker image digests; omission is unverified",
    )
    args = parser.parse_args()
    if min(args.limit, args.repeats, args.max_turns, args.max_tokens) < 1 or args.timeout <= 0:
        parser.error("budgets must be positive")
    try:
        asyncio.run(run(args))
    except (httpx.HTTPError, ValueError) as exc:
        parser.exit(1, f"AgentBench preflight failed: {type(exc).__name__}: {exc}\n")


if __name__ == "__main__":
    main()
