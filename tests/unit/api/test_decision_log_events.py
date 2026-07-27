"""批 3 ⑦：hook_translator 里 decision_log 的两个死分支。

PreToolUse 载荷的 tool_name 是**客户端全名**（mcp__ai-team-os__meeting_create），
而分支拿裸名字面量比对：

1. ``tool_name == "meeting_start"`` —— 工具根本不叫这个名（真名 meeting_create），
   名字对了前缀也还差着，双重错位；
2. ``tool_name == "task_run"`` —— 差一个 mcp__ai-team-os__ 前缀。

两个分支从上线起没进过一次，decision.* 决策事件流长期空转。剥前缀用
always_load.normalize_tool_name（现成的；已删除的 pipeline/tool_classifier 不可引用）。
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from aiteam.api.event_bus import EventBus
from aiteam.api.hook_translator import HookTranslator
from aiteam.storage.connection import close_db
from aiteam.storage.repository import StorageRepository

SESSION = "sess-decision-1"


@pytest_asyncio.fixture()
async def translator():
    repo = StorageRepository(db_url="sqlite+aiosqlite://")
    await repo.init_db()
    ht = HookTranslator(repo=repo, event_bus=EventBus(repo=repo))
    yield ht
    await close_db()


async def _run(ht: HookTranslator, tool_name: str, tool_input: dict) -> list[tuple[str, dict]]:
    captured: list[tuple[str, dict]] = []

    async def fake_emit(event_type, source, data, *args, **kwargs):
        captured.append((event_type, data))

    ht.event_bus.emit = fake_emit  # type: ignore[method-assign]
    await ht._on_pre_tool_use(
        {
            "tool_name": tool_name,
            "session_id": SESSION,
            "tool_input": tool_input,
            "agent_type": "team-lead",
        }
    )
    return captured


@pytest.mark.asyncio
async def test_meeting_create_emits_decision_event(translator):
    captured = await _run(
        translator,
        "mcp__ai-team-os__meeting_create",
        {"topic": "批 3 收口评审", "participants": ["a", "b"], "purpose": "定方案"},
    )
    decisions = [d for t, d in captured if t == "decision.meeting_started"]
    assert len(decisions) == 1
    assert decisions[0]["topic"] == "批 3 收口评审"
    assert decisions[0]["participants"] == ["a", "b"]


@pytest.mark.asyncio
async def test_task_run_emits_decision_event(translator):
    captured = await _run(
        translator,
        "mcp__ai-team-os__task_run",
        {"title": "修 decision_log", "agent_name": "fixer", "description": "缘由"},
    )
    decisions = [d for t, d in captured if t == "decision.task_assigned"]
    assert len(decisions) == 1
    assert decisions[0]["task_title"] == "修 decision_log"
    assert decisions[0]["assigned_to"] == "fixer"


@pytest.mark.asyncio
async def test_bare_tool_name_still_works(translator):
    """裸名（无前缀）调用也认——归一化是幂等的。"""
    captured = await _run(translator, "meeting_create", {"topic": "裸名"})
    assert len([d for t, d in captured if t == "decision.meeting_started"]) == 1


@pytest.mark.asyncio
async def test_unrelated_tool_emits_no_decision(translator):
    captured = await _run(translator, "Read", {"file_path": "/tmp/x"})
    assert not [t for t, _ in captured if t.startswith("decision.")]


@pytest.mark.asyncio
async def test_retired_meeting_start_name_is_gone(translator):
    """旧字面量 meeting_start 不是真工具名，不该再有任何分支认它。"""
    import inspect

    src = inspect.getsource(HookTranslator._on_pre_tool_use)
    assert '"meeting_start"' not in src
    assert '"meeting_create"' in src
