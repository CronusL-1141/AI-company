"""CC hook 事件建的行从 09-30 起标 harness=claude-code（缔造者裁定 P-4）。

每个宿主有自己的 hook 入口：Codex 入口在载荷里写 harness=codex，CC 入口
send_event.py 什么都不写。服务端据此判定，不必改 hook 副本。此前 CC 行一律是
NULL，于是覆盖率矩阵里"未标注 harness"那一桶其实就是全部 CC 行（报告 c20b37a8 W8）。
断言一律查库。
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from aiteam.api.event_bus import EventBus
from aiteam.api.hook_translator import WORKFLOW_AGENT_TYPE, HookTranslator, _payload_harness
from aiteam.storage.connection import close_db
from aiteam.storage.repository import StorageRepository
from aiteam.types import HarnessId

SESSION = "39c7d5a3-52c6-4735-b866-1fa68229709a"
CWD = "/Users/dev/Desktop/AI team OS"


@pytest_asyncio.fixture()
async def repo():
    r = StorageRepository(db_url="sqlite+aiosqlite://")
    await r.init_db()
    yield r
    await close_db()


@pytest_asyncio.fixture()
async def translator(repo):
    await repo.create_project(name="AI team OS", root_path=CWD)
    yield HookTranslator(repo=repo, event_bus=EventBus(repo=repo))


async def _all_agents(repo):
    return [a for t in await repo.list_teams() for a in await repo.list_agents(t.id)]


def test_the_payload_decides_by_entry_script():
    assert _payload_harness({"harness": "codex"}) is HarnessId.CODEX
    assert _payload_harness({"hook_event_name": "SessionStart"}) is HarnessId.CLAUDE_CODE


@pytest.mark.asyncio
async def test_cc_session_leader_and_subagents_are_labelled(repo, translator):
    await translator.handle_event({"hook_event_name": "SessionStart", "session_id": SESSION, "cwd": CWD})
    await translator.handle_event({
        "hook_event_name": "SubagentStart", "session_id": SESSION, "cwd": CWD,
        "agent_id": "a7bf3e26417e47c19", "agent_type": "general-purpose",
    })
    await translator.handle_event({
        "hook_event_name": "SubagentStart", "session_id": SESSION, "cwd": CWD,
        "agent_id": "a11e36b3fd54b8ed6", "agent_type": WORKFLOW_AGENT_TYPE,
    })

    rows = {a.role: a for a in await _all_agents(repo)}
    assert set(rows) == {"leader", "general-purpose", WORKFLOW_AGENT_TYPE}
    for role, row in rows.items():
        stored = await repo.get_agent(row.id)
        assert stored.harness == HarnessId.CLAUDE_CODE, f"{role} 行没有标 claude-code"


@pytest.mark.asyncio
async def test_leader_born_at_first_activity_is_labelled_too(repo, translator):
    """空会话（source=startup）的 Leader 在第一次活动时才建（任务 cb884c72）；两条出生路径
    共用 _birth_session_leader，标注写在那里，这一条路径同样是 claude-code。"""
    await translator.handle_event({
        "hook_event_name": "SessionStart", "session_id": SESSION, "cwd": CWD, "source": "startup",
    })
    assert not [a for a in await _all_agents(repo) if a.role == "leader"], "空会话不该先建 Leader"

    await translator.handle_event({
        "hook_event_name": "PreToolUse", "session_id": SESSION, "cwd": CWD,
        "tool_name": "Bash", "tool_input": {"command": "pwd"}, "tool_use_id": "toolu_first",
    })
    leaders = [a for a in await _all_agents(repo) if a.role == "leader"]
    assert len(leaders) == 1
    assert (await repo.get_agent(leaders[0].id)).harness == HarnessId.CLAUDE_CODE
