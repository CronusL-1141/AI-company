"""成员卡「正在使用 X」改读 agent_activities，intent.agent_working 停写。

intent.agent_working 是 agent_activities 的派生流水：PreToolUse 每次先落一条活动，
再对 Read/Edit/Write/Bash 按 agent 10 秒节流补发一条同内容事件。唯一的读者是成员卡的
``/api/teams/{id}/agent-intents``；decisions 的默认合并也会把它捞进来，而它的 source
是 ``agent:<id>``，按 team 过滤时全被丢掉，结果只是在 limit 里挤掉真正的决策事件。

这里全程走 HTTP：hook 事件从 ``POST /api/hooks/event`` 进，成员卡数据从 GET 读回，
中间隔着落库，漏写字段或读错表都会在这里现形。
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import pytest
from fastapi.testclient import TestClient

from aiteam.api import deps
from aiteam.api.app import create_app
from aiteam.api.event_bus import EventBus
from aiteam.api.hook_translator import HookTranslator
from aiteam.memory.store import MemoryStore
from aiteam.orchestrator.team_manager import TeamManager
from aiteam.storage.connection import close_db
from aiteam.storage.repository import StorageRepository
from aiteam.types import AgentStatus

SESSION = "sess-intent-1"
CC_ID = "cc-sub-intent-1"

# The member card (dashboard/src/api/decisions.ts AgentIntent) reads exactly these keys.
CONTRACT_KEYS = {
    "agent_id",
    "agent_name",
    "tool_name",
    "intent_summary",
    "input_preview",
    "timestamp",
}


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


@pytest.fixture()
def env():
    # One repository behind every singleton, so hook writes and route reads meet.
    repo = StorageRepository(db_url="sqlite+aiosqlite://")
    _run(repo.init_db())
    memory = MemoryStore(repository=repo)
    bus = EventBus(repo=repo)
    deps._repository = repo
    deps._memory_store = memory
    deps._event_bus = bus
    deps._manager = TeamManager(repository=repo, memory=memory)
    deps._hook_translator = HookTranslator(repo=repo, event_bus=bus)

    app = create_app()

    @asynccontextmanager
    async def no_lifespan(app):
        yield

    app.router.lifespan_context = no_lifespan

    team = _run(repo.create_team(name="intent-team", mode="coordinate"))
    worker = _run(
        repo.create_agent(
            team_id=team.id, name="worker", role="backend", cc_tool_use_id=CC_ID,
            session_id=SESSION,
        )
    )
    _run(repo.update_agent(worker.id, status=AgentStatus.BUSY))
    idle = _run(repo.create_agent(team_id=team.id, name="idler", role="qa"))
    _run(repo.update_agent(idle.id, status=AgentStatus.WAITING))

    yield {
        "client": TestClient(app),
        "repo": repo,
        "team": team,
        "worker": worker,
        "idle": idle,
    }

    _run(close_db())
    deps._repository = None
    deps._memory_store = None
    deps._event_bus = None
    deps._manager = None
    deps._hook_translator = None


def _pre_tool_use(client: TestClient, tool_name: str, tool_input: dict) -> None:
    resp = client.post(
        "/api/hooks/event",
        json={
            "hook_event_name": "PreToolUse",
            "session_id": SESSION,
            "agent_id": CC_ID,
            "agent_type": "worker",
            "tool_name": tool_name,
            "tool_input": tool_input,
        },
    )
    assert resp.status_code == 200, resp.text


def _intents(client: TestClient, team_id: str) -> list[dict]:
    resp = client.get(f"/api/teams/{team_id}/agent-intents")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["success"] is True
    return body["data"]


def test_pre_tool_use_no_longer_writes_intent_events(env):
    _pre_tool_use(env["client"], "Bash", {"command": "pytest -q"})
    _pre_tool_use(env["client"], "Read", {"file_path": "/repo/a.py"})

    written = _run(env["repo"].list_events(event_type="intent.agent_working", limit=10))
    assert written == []
    # The activity rows the member card now reads are still recorded.
    activities = _run(env["repo"].list_activities(env["worker"].id, limit=10))
    assert [a.tool_name for a in activities] == ["Read", "Bash"]


def test_member_card_shows_the_latest_activity(env):
    client, team, worker = env["client"], env["team"], env["worker"]
    _pre_tool_use(client, "Read", {"file_path": "/repo/a.py"})
    # Inside what used to be the 10 s throttle window: the card must still move on.
    _pre_tool_use(client, "Bash", {"command": "pytest -q tests/unit"})

    rows = _intents(client, team.id)
    assert len(rows) == 1
    row = rows[0]
    assert set(row) == CONTRACT_KEYS
    assert row["agent_id"] == worker.id
    assert row["agent_name"] == "worker"
    assert row["tool_name"] == "Bash"
    assert row["intent_summary"] == "正在使用 Bash"
    assert row["input_preview"] == "pytest -q tests/unit"
    assert row["timestamp"]


def test_input_preview_is_capped_at_100_chars(env):
    long_command = "echo " + "x" * 300
    _pre_tool_use(env["client"], "Bash", {"command": long_command})

    row = _intents(env["client"], env["team"].id)[0]
    assert row["input_preview"] == long_command[:100]


def test_busy_agent_without_activity_keeps_blank_contract(env):
    """Busy but nothing recorded yet: same blank row as before, no idle agents listed."""
    rows = _intents(env["client"], env["team"].id)

    assert rows == [
        {
            "agent_id": env["worker"].id,
            "agent_name": "worker",
            "tool_name": "",
            "intent_summary": "",
            "input_preview": "",
            "timestamp": None,
        }
    ]


def test_decisions_default_merge_ignores_intent_history(env):
    """Historical intent rows must not crowd team decisions out of the default page."""
    repo, team, worker = env["repo"], env["team"], env["worker"]
    _run(
        repo.create_event(
            "decision.task_assigned", f"team:{team.id}", {"task_title": "ship it"}
        )
    )
    for _ in range(2):
        _run(
            repo.create_event(
                "intent.agent_working",
                f"agent:{worker.id}",
                {"agent_id": worker.id, "tool_name": "Bash", "intent_summary": "正在使用 Bash"},
            )
        )

    resp = env["client"].get(f"/api/decisions?team_id={team.id}&limit=2")
    assert resp.status_code == 200, resp.text
    assert [e["type"] for e in resp.json()["data"]] == ["decision.task_assigned"]

    unfiltered = env["client"].get("/api/decisions?limit=10").json()["data"]
    assert all(not e["type"].startswith("intent.") for e in unfiltered)

    # History stays queryable on explicit request.
    explicit = env["client"].get("/api/decisions?type=intent.&limit=10").json()["data"]
    assert [e["type"] for e in explicit] == ["intent.agent_working"] * 2


def test_member_card_drops_the_mcp_server_prefix(env):
    """`mcp__<server>__<tool>` is shown as `<tool>`; the raw name stays in tool_name."""
    _pre_tool_use(env["client"], "mcp__ai-team-os__task_memo_add", {"task_id": "t-1"})

    row = _intents(env["client"], env["team"].id)[0]
    assert row["tool_name"] == "mcp__ai-team-os__task_memo_add"
    assert row["intent_summary"] == "正在使用 task_memo_add"
