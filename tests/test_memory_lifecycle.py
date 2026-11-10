import json
import time

import pytest

from tests.test_memory_extractor import FakeProvider
from xclaw.ai.types import ChatResponse
from xclaw.coding import CodingAgent
from xclaw.core.memory import ConversationMemory
from xclaw.core.memory_extractor import MemoryExtractor
from xclaw.telemetry import current_model_purpose


def operation(action="upsert", **kwargs):
    return {
        "action": action,
        "key": "project_language",
        "scope": "project",
        "evidence": "项目以后改用 Java",
        **kwargs,
    }


def test_default_storage_retains_more_than_fifty_records_across_restart(tmp_path):
    memory = ConversationMemory()
    for i in range(120):
        memory.remember(f"fact_{i}", str(i))
    path = tmp_path / "memory.json"
    memory.save(str(path))
    restored = ConversationMemory()
    restored.load(str(path))
    assert len(restored) == 120
    assert restored.recall("fact_0") == "0"


def test_replacement_removes_old_value_from_storage_prompt_and_saved_file(tmp_path):
    memory = ConversationMemory()
    memory.remember("project_language", "Python", scope="project:A")
    memory.remember("project_language", "Java", scope="project:A")
    assert len(memory) == 1
    assert "Python" not in memory.format_for_prompt(scope="project:A")
    path = tmp_path / "memory.json"
    memory.save(str(path))
    assert "Python" not in path.read_text(encoding="utf-8")


def test_replacement_and_forgetting_do_not_affect_other_projects():
    memory = ConversationMemory()
    memory.remember("language", "Python", scope="project:A")
    memory.remember("language", "Go", scope="project:B")
    memory.remember("language", "Java", scope="project:A")
    assert "Go" not in memory.format_for_prompt(scope="project:A")
    assert memory.forget("language", scope="project:A")
    assert memory.recall("language", scope="project:B") == "Go"


def test_legacy_cleanup_keeps_newest_fact_and_does_not_resurrect_expired_old_values(tmp_path):
    path = tmp_path / "old.json"
    path.write_text(
        json.dumps(
            [
                {"type": "kv", "key": "language", "value": "Python"},
                {"type": "kv", "key": "language", "value": "Java"},
                {"type": "kv", "key": "temporary", "value": "old"},
                {"type": "kv", "key": "temporary", "value": "new", "expires_at": time.time() - 1},
                {"type": "kv", "key": "broken"},
                {"type": "event", "content": ""},
                42,
            ]
        ),
        encoding="utf-8",
    )
    memory = ConversationMemory()
    memory.load(str(path))
    assert memory.get_all() == [{"type": "kv", "key": "language", "value": "Java"}]


def test_expired_update_removes_old_fact_instead_of_reviving_it():
    memory = ConversationMemory()
    memory.remember("temporary", "old")
    memory.remember("temporary", "new", expires_at=time.time() - 1)
    assert memory.recall("temporary") is None


def test_prompt_budget_does_not_delete_stored_records():
    memory = ConversationMemory()
    for i in range(120):
        memory.remember(str(i), "important " * 20)
    prompt = memory.format_for_prompt(max_chars=400)
    assert len(prompt) <= 400
    assert len(memory) == 120


@pytest.mark.parametrize(
    "bad",
    [
        {"evidence": "invented quote"},
        {"action": ["forget"]},
        {"scope": {"project": "elsewhere"}},
        {"action": "clear_all"},
    ],
)
async def test_reconciliation_rejects_ungrounded_or_malformed_operations(bad):
    provider = FakeProvider(json.dumps([operation(value="Java", **bad)]))
    result = await MemoryExtractor(provider).extract("项目以后改用 Java", existing=[])
    assert result == []


async def test_unknown_forget_is_rejected_and_existing_namespace_is_preserved():
    provider = FakeProvider(json.dumps([operation(value="Java")]))
    extractor = MemoryExtractor(provider)
    result = await extractor.extract(
        "项目以后改用 Java",
        existing=[
            {"key": "project_language", "value": "Python", "scope": "user"},
        ],
    )
    assert result[0]["scope"] == "user"
    provider.content = json.dumps([operation(action="forget")])
    assert await extractor.extract("项目以后改用 Java", existing=[]) == []


@pytest.mark.parametrize("fail_main", [False, True])
async def test_agent_reconciles_before_main_request_and_keeps_update_if_task_fails(
    tmp_path, fail_main
):
    class Provider(FakeProvider):
        async def chat(self, messages, tools=None, **kwargs):
            if current_model_purpose() == "memory":
                payload = json.loads(messages[-1].content)
                assert payload["user_input"] == "项目以后改用 Java"
                assert payload["existing"][0]["value"] == "Python"
                return ChatResponse(content=json.dumps([operation(value="Java")]))
            current = messages[-1].content
            assert "project_language: Java" in current
            assert "project_language: Python" not in current
            if fail_main:
                raise RuntimeError("simulated task failure")
            return ChatResponse(content="已收到要求")

    agent = CodingAgent(
        Provider(), auto_memory=True, workspace_dir=str(tmp_path), register_default_tools=False
    )
    agent.client.MAX_RETRIES = 1
    agent.remember("project_language", "Python", scope=agent.memory_scope)
    if fail_main:
        with pytest.raises(RuntimeError):
            await agent.run("项目以后改用 Java")
    else:
        await agent.run("项目以后改用 Java")
    assert agent.recall("project_language", scope=agent.memory_scope) == "Java"
    await agent.sandbox_router.stop()
