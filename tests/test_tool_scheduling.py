"""调度必须真的并发、尊重依赖，并在模型错误或前置失败时保守执行。"""

import asyncio
import json
import threading

import pytest

from xclaw.ai.provider import BaseProvider
from xclaw.ai.types import ChatMessage, ChatResponse, FunctionCall, ToolCall
from xclaw.coding import CodingAgent
from xclaw.core.concurrency import ReadWriteGate
from xclaw.core.execution import Capability
from xclaw.core.scheduling import PLANNING_PROMPT, BatchPlan, SchedulingConfig
from xclaw.core.tool import Tool
from xclaw.im.delivery import collect_deliveries, register_delivery_tool
from xclaw.server.config import AgentConfig


def call(call_id, name="read", **kwargs):
    return ToolCall(id=call_id, function=FunctionCall(name=name, arguments=json.dumps(kwargs)))


def plan(mode, *steps):
    return json.dumps({"mode": mode, "steps": [
        {"id": step[0], "depends_on": step[1], "defer": step[2] if len(step) > 2 else False}
        for step in steps
    ]})


class SchedulingProvider(BaseProvider):
    def __init__(self, decision, responses=()):
        self.decision = decision
        self.responses = list(responses)
        self.requests = []

    async def chat(self, messages, tools=None, **kwargs):
        self.requests.append((messages, tools, kwargs))
        if messages[0].content == PLANNING_PROMPT:
            assert tools is None
            if isinstance(self.decision, Exception):
                raise self.decision
            if callable(self.decision):
                return await self.decision()
            return ChatResponse(content=self.decision)
        return self.responses.pop(0)

    async def chat_stream(self, *args, **kwargs):
        raise AssertionError("Unexpected streaming")
        yield


def agent(tmp_path, decision, **options):
    return CodingAgent(SchedulingProvider(decision), workspace_dir=str(tmp_path),
                       register_default_tools=False, **options)


def read_tool(func, name="read", **options):
    return Tool(name, "读取数据", func, capabilities=frozenset({Capability.FILE_READ}),
                parallel_safe=True, **options)


async def test_llm_parallel_reads_really_overlap_through_router_and_keep_result_order(tmp_path):
    bot = agent(tmp_path, plan("parallel", ("a", []), ("b", [])))
    both_started = asyncio.Event()
    starts = []

    async def read(label: str):
        starts.append(label)
        if len(starts) == 2:
            both_started.set()
        await asyncio.wait_for(both_started.wait(), 1)
        return label

    bot.register_tool(read_tool(read))
    results = await bot._act([call("a", label="A"), call("b", label="B")])
    assert starts == ["A", "B"]
    assert [r.content for r in results] == ["A", "B"]
    assert [r.tool_call_id for r in results] == ["a", "b"]
    assert bot.client.get_history() == []


async def test_llm_serial_choice_prevents_read_overlap(tmp_path):
    bot = agent(tmp_path, plan("serial", ("b", []), ("a", [])))
    events = []

    async def read(label: str):
        events.append(f"start-{label}")
        await asyncio.sleep(0)
        events.append(f"end-{label}")
        return label

    bot.register_tool(read_tool(read))
    results = await bot._act([call("a", label="A"), call("b", label="B")])
    assert events == ["start-B", "end-B", "start-A", "end-A"]
    assert [r.content for r in results] == ["A", "B"]


async def test_dag_executes_independent_roots_before_consumer(tmp_path):
    bot = agent(tmp_path, plan("dag", ("c", ["a", "b"]), ("a", []), ("b", [])))
    completed = set()
    both_started = asyncio.Event()
    starts = []

    async def read(label: str):
        starts.append(label)
        if label != "c":
            if len(starts) == 2:
                both_started.set()
            await asyncio.wait_for(both_started.wait(), 1)
        else:
            assert completed == {"a", "b"}
        completed.add(label)
        return label

    bot.register_tool(read_tool(read))
    results = await bot._act([call(k, label=k) for k in ("c", "a", "b")])
    assert starts == ["a", "b", "c"]
    assert [r.content for r in results] == ["c", "a", "b"]


async def test_parallel_limit_is_enforced(tmp_path):
    ids = [str(i) for i in range(7)]
    bot = agent(tmp_path, plan("parallel", *((i, []) for i in ids)),
                tool_scheduling=SchedulingConfig(max_parallel=2))
    active = maximum = 0

    async def read(label: str):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        await asyncio.sleep(0.01)
        active -= 1
        return label

    bot.register_tool(read_tool(read))
    await bot._act([call(i, label=i) for i in ids])
    assert maximum == 2


@pytest.mark.parametrize("decision", [
    "not json", plan("parallel", ("a", [])),
    plan("dag", ("a", ["missing"]), ("b", [])),
    plan("dag", ("a", ["b"]), ("b", ["a"])),
    plan("dag", ("a", ["a"]), ("b", [])),
    plan("parallel", ("a", []), ("a", [])),
    RuntimeError("planner unavailable"),
])
async def test_invalid_or_failed_plan_falls_back_to_serial_without_losing_calls(tmp_path, decision):
    bot = agent(tmp_path, decision)
    events = []

    async def read(label: str):
        events.append(label)
        await asyncio.sleep(0)
        events.append(label)
        return label

    bot.register_tool(read_tool(read))
    results = await bot._act([call("a", label="A"), call("b", label="B")])
    assert events == ["A", "A", "B", "B"]
    assert len(results) == 2


async def test_planning_timeout_cancels_planner_and_falls_back(tmp_path):
    cancelled = asyncio.Event()

    async def hang():
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    bot = agent(tmp_path, hang, tool_scheduling=SchedulingConfig(planning_timeout=0.02))

    async def read():
        return "ok"

    bot.register_tool(read_tool(read))
    assert len(await bot._act([call("a"), call("b")])) == 2
    assert cancelled.is_set()


async def test_failed_prerequisite_skips_consumer_but_independent_call_completes(tmp_path):
    bot = agent(tmp_path, plan("dag", ("a", []), ("b", ["a"]), ("c", [])))
    executed = []

    async def read(label: str):
        executed.append(label)
        if label == "a":
            raise ValueError("missing data")
        return label

    bot.register_tool(read_tool(read))
    results = await bot._act([call(k, label=k) for k in ("a", "b", "c")])
    assert executed == ["a", "c"]
    assert "前置调用失败" in results[1].content
    assert results[2].content == "c"


async def test_result_dependent_arguments_are_deferred(tmp_path):
    bot = agent(tmp_path, plan("dag", ("a", []), ("b", ["a"], True)))
    executed = []

    async def read(label: str):
        executed.append(label)
        return "real-id"

    bot.register_tool(read_tool(read))
    results = await bot._act([call("a", label="query"), call("b", label="guessed-id")])
    assert executed == ["query"]
    assert "参数需要根据前置" in results[1].content


async def test_side_effects_stay_serial_even_if_model_requests_parallel(tmp_path):
    bot = agent(tmp_path, plan("parallel", ("a", []), ("b", [])))
    events = []

    async def write(label: str):
        events.append(label)
        await asyncio.sleep(0)
        events.append(label)
        return label

    bot.register_tool(Tool("write", "写入", write, parallel_safe=True,
                           capabilities=frozenset({Capability.FILE_WRITE})))
    await bot._act([call("a", "write", label="A"), call("b", "write", label="B")])
    assert events == ["A", "A", "B", "B"]


async def test_router_exclusive_operation_waits_for_all_readers(tmp_path):
    bot = agent(tmp_path, "")
    starts = 0
    entered = asyncio.Event()
    release = asyncio.Event()
    events = []

    async def read():
        nonlocal starts
        starts += 1
        if starts == 2:
            entered.set()
        await release.wait()
        events.append("read-done")
        return "ok"

    async def write():
        events.append("write")
        return "ok"

    bot.register_tool(read_tool(read))
    bot.register_tool(Tool("write", "写入", write,
                           capabilities=frozenset({Capability.FILE_WRITE})))
    readers = [asyncio.create_task(bot.tool_registry.execute("read", "{}")) for _ in range(2)]
    try:
        await asyncio.wait_for(entered.wait(), 1)
        writer = asyncio.create_task(bot.tool_registry.execute("write", "{}"))
        await asyncio.sleep(0)
        assert events == []
        release.set()
        await asyncio.gather(*readers, writer)
        assert events == ["read-done", "read-done", "write"]
    finally:
        release.set()
        await asyncio.gather(*readers)


async def test_sync_parallel_safe_functions_use_threads(tmp_path):
    bot = agent(tmp_path, plan("parallel", ("a", []), ("b", [])))
    barrier = threading.Barrier(2, timeout=2)

    def read(label: str):
        barrier.wait()
        return label

    bot.register_tool(read_tool(read))
    results = await bot._act([call("a", label="A"), call("b", label="B")])
    assert [r.content for r in results] == ["A", "B"]


async def test_excel_delivery_is_reordered_after_creation_even_if_llm_misses_dependency(tmp_path):
    pytest.importorskip("openpyxl")
    from xclaw.coding.excel_tool import register_excel_tool

    bot = agent(tmp_path, plan("parallel", ("send", []), ("create", [])))
    register_excel_tool(bot.tool_registry)
    register_delivery_tool(bot.tool_registry)
    with collect_deliveries() as files:
        results = await bot._act([
            call("send", "deliver_file", path="students.xlsx"),
            call("create", "create_excel", path="students.xlsx", columns=["学号"], rows=[["001"]]),
        ])
        assert files == [tmp_path / "students.xlsx"]
    assert "最终附件已准备" in results[0].content


async def test_failed_excel_creation_cannot_deliver_stale_existing_file(tmp_path):
    pytest.importorskip("openpyxl")
    from xclaw.coding.excel_tool import register_excel_tool

    bot = agent(tmp_path, plan("parallel", ("send", []), ("create", [])))
    register_excel_tool(bot.tool_registry)
    register_delivery_tool(bot.tool_registry)
    args = {"path": "students.xlsx", "columns": ["学号"], "rows": [["001"]]}
    assert "执行失败" not in await bot.tool_registry.execute("create_excel", json.dumps(args))
    with collect_deliveries() as files:
        results = await bot._act([
            call("send", "deliver_file", path="students.xlsx"),
            call("create", "create_excel", **args),
        ])
        assert files == []
    assert "前置调用失败" in results[0].content


async def test_duplicate_deliveries_do_not_create_artificial_cycle(tmp_path):
    pytest.importorskip("openpyxl")
    from xclaw.coding.excel_tool import register_excel_tool

    bot = agent(tmp_path, plan("parallel", ("send-a", []), ("send-b", []), ("create", [])))
    register_excel_tool(bot.tool_registry)
    register_delivery_tool(bot.tool_registry)
    with collect_deliveries() as files:
        results = await bot._act([
            call("send-a", "deliver_file", path="students.xlsx"),
            call("send-b", "deliver_file", path="students.xlsx"),
            call("create", "create_excel", path="students.xlsx", columns=["学号"], rows=[["001"]]),
        ])
        assert files == [tmp_path / "students.xlsx"]
    assert all("最终附件已准备" in r.content for r in results[:2])


async def test_delivery_waits_for_check_and_skips_if_check_fails(tmp_path):
    bot = agent(tmp_path, plan("parallel", ("send", []), ("check", [])))
    register_delivery_tool(bot.tool_registry)

    async def check(path: str):
        return "❌ 工作簿检查失败"

    bot.register_tool(read_tool(check, "check", path_parameters=("path",)))
    with collect_deliveries() as files:
        results = await bot._act([
            call("send", "deliver_file", path="students.xlsx"),
            call("check", "check", path="students.xlsx"),
        ])
        assert files == []
    assert "前置调用失败" in results[0].content


async def test_single_call_and_disabled_planning_make_no_extra_model_request(tmp_path):
    bot = agent(tmp_path, RuntimeError("must not request"),
                tool_scheduling=SchedulingConfig(enabled=False))

    async def read():
        return "ok"

    bot.register_tool(read_tool(read))
    await bot._act([call("a")])
    await bot._act([call("a"), call("b")])
    assert bot.client.provider.requests == []


async def test_main_loop_keeps_tool_protocol_and_planning_separate(tmp_path):
    calls = [call("a", label="A"), call("b", label="B")]
    provider = SchedulingProvider(plan("parallel", ("a", []), ("b", [])),
                                  [ChatResponse(tool_calls=calls, reasoning_content="state"),
                                   ChatResponse(content="完成")])
    bot = CodingAgent(provider, workspace_dir=str(tmp_path), register_default_tools=False)

    async def read(label: str):
        return label

    bot.register_tool(read_tool(read))
    assert await bot.run("比较两个数据") == "完成"
    messages, _, _ = provider.requests[-1]
    assert [m.role for m in messages] == ["system", "user", "assistant", "tool", "tool"]
    assert messages[2].reasoning_content == "state"
    assert [m.tool_call_id for m in messages[3:]] == ["a", "b"]
    assert bot.client.get_history() == [ChatMessage(role="user", content="比较两个数据"),
                                        ChatMessage(role="assistant", content="完成")]


async def test_cancelled_waiting_writer_does_not_block_future_reads():
    gate = ReadWriteGate()
    async with gate.hold(shared=True):
        async def write():
            async with gate.hold(shared=False):
                pass
        waiter = asyncio.create_task(write())
        await asyncio.sleep(0)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        async with asyncio.timeout(1):
            async with gate.hold(shared=True):
                pass


async def test_cancelled_thread_read_keeps_gate_until_background_work_exits(tmp_path):
    bot = agent(tmp_path, "")
    started = threading.Event()
    release = threading.Event()
    wrote = asyncio.Event()

    def read():
        started.set()
        release.wait(timeout=2)
        raise ValueError("read failed after cancellation")

    async def write():
        wrote.set()
        return "ok"

    bot.register_tool(read_tool(read))
    bot.register_tool(Tool("write", "写入", write,
                           capabilities=frozenset({Capability.FILE_WRITE})))
    reader = asyncio.create_task(bot.tool_registry.execute("read", "{}"))
    writer = None
    try:
        assert await asyncio.to_thread(started.wait, 1)
        reader.cancel()
        writer = asyncio.create_task(bot.tool_registry.execute("write", "{}"))
        await asyncio.sleep(0.01)
        assert not wrote.is_set()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await reader
        assert await asyncio.wait_for(writer, 1) == "ok"
    finally:
        release.set()
        await asyncio.gather(reader, *([writer] if writer else []), return_exceptions=True)


def test_config_validates_limits_and_plan_rejects_parameter_rewrites():
    assert AgentConfig().tool_scheduling.enabled
    for kwargs in ({"max_parallel": 0}, {"max_parallel": 17}, {"planning_timeout": float("inf")}):
        with pytest.raises(ValueError):
            SchedulingConfig(**kwargs)
    with pytest.raises(ValueError):
        BatchPlan.model_validate_json(json.dumps({"mode": "parallel", "steps": [
            {"id": "a", "depends_on": [], "arguments": {"path": "other"}},
        ]}))
