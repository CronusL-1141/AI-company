"""Tests for meeting security: impersonation audit and conclude attendance validation."""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient
from testlib import make_team

from aiteam.api import deps
from aiteam.api.app import create_app
from aiteam.api.event_bus import EventBus
from aiteam.api.hook_translator import HookTranslator
from aiteam.memory.store import MemoryStore
from aiteam.orchestrator.team_manager import TeamManager
from aiteam.storage.connection import close_db
from aiteam.storage.repository import StorageRepository


def _make_client():
    """Create a test client with in-memory SQLite and mocked event bus."""
    from contextlib import asynccontextmanager
    from unittest.mock import AsyncMock

    repo = StorageRepository(db_url="sqlite+aiosqlite://")
    asyncio.get_event_loop().run_until_complete(repo.init_db())
    memory = MemoryStore(repository=repo)
    manager = TeamManager(repository=repo, memory=memory)
    event_bus = EventBus(repo=repo)
    event_bus.emit = AsyncMock(return_value=None)
    hook_translator = HookTranslator(repo=repo, event_bus=event_bus)

    deps._repository = repo
    deps._memory_store = memory
    deps._event_bus = event_bus
    deps._manager = manager
    deps._hook_translator = hook_translator

    app = create_app()

    @asynccontextmanager
    async def test_lifespan(app):
        yield

    app.router.lifespan_context = test_lifespan
    return TestClient(app), repo, event_bus


def _teardown():
    asyncio.get_event_loop().run_until_complete(close_db())
    deps._repository = None
    deps._memory_store = None
    deps._event_bus = None
    deps._manager = None
    deps._hook_translator = None


# ============================================================
# Repository-level: msg_metadata persistence
# ============================================================


@pytest.mark.asyncio()
async def test_msg_metadata_persisted():
    """msg_metadata is stored and retrieved correctly from DB."""
    from aiteam.storage.connection import close_db as _close

    repo = StorageRepository(db_url="sqlite+aiosqlite://")
    await repo.init_db()

    team = await repo.create_team(name="meta-team", mode="coordinate")
    m = await repo.create_meeting(
        team_id=team.id,
        topic="meta test",
        participants=["agent-a"],
    )
    msg = await repo.create_meeting_message(
        meeting_id=m.id,
        agent_id="arch-lead",
        agent_name="arch-lead",
        content="Proxied message",
        round_number=1,
        msg_metadata={"impersonation": True, "actual_author": "team-lead"},
    )
    assert msg.msg_metadata == {"impersonation": True, "actual_author": "team-lead"}

    messages = await repo.list_meeting_messages(m.id)
    assert messages[0].msg_metadata.get("impersonation") is True
    assert messages[0].msg_metadata.get("actual_author") == "team-lead"
    await _close()


@pytest.mark.asyncio()
async def test_empty_msg_metadata_default():
    """msg_metadata defaults to empty dict when not specified."""
    from aiteam.storage.connection import close_db as _close

    repo = StorageRepository(db_url="sqlite+aiosqlite://")
    await repo.init_db()

    team = await repo.create_team(name="default-meta-team", mode="coordinate")
    m = await repo.create_meeting(
        team_id=team.id,
        topic="default meta test",
        participants=["agent-a"],
    )
    msg = await repo.create_meeting_message(
        meeting_id=m.id,
        agent_id="agent-a",
        agent_name="agent-a",
        content="Regular message",
        round_number=1,
    )
    assert msg.msg_metadata == {}
    await _close()


# ============================================================
# HTTP: Impersonation detection
# ============================================================


def test_impersonation_flagged_via_http():
    """caller_agent_id='team-lead' != agent_id='arch-lead' → 201 + impersonation audit."""
    client, repo, event_bus = _make_client()
    try:
        team_id = make_team({"name": "imp-team", "mode": "coordinate"})["id"]

        mtg_resp = client.post(
            f"/api/teams/{team_id}/meetings",
            json={"topic": "impersonation test", "participants": ["arch-lead"]},
        )
        meeting_id = mtg_resp.json()["data"]["id"]

        resp = client.post(
            f"/api/meetings/{meeting_id}/messages",
            json={
                "agent_id": "arch-lead",
                "agent_name": "arch-lead",
                "content": "Leader speaking as arch-lead",
                "round_number": 1,
                "caller_agent_id": "team-lead",
            },
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["success"] is True
        # Response message signals impersonation audit
        assert "代打" in data["message"]

        # Verify metadata stored
        messages = asyncio.get_event_loop().run_until_complete(
            repo.list_meeting_messages(meeting_id)
        )
        assert messages[0].msg_metadata.get("impersonation") is True
        assert messages[0].msg_metadata.get("actual_author") == "team-lead"

        # Verify impersonation event was emitted
        emitted = [call.args[0] for call in event_bus.emit.call_args_list]
        assert "meeting.impersonation" in emitted
    finally:
        _teardown()


def test_no_impersonation_when_caller_matches():
    """caller_agent_id == agent_id → no impersonation marker."""
    client, repo, event_bus = _make_client()
    try:
        team_id = make_team({"name": "legit-team", "mode": "coordinate"})["id"]

        mtg_resp = client.post(
            f"/api/teams/{team_id}/meetings",
            json={"topic": "legit test", "participants": ["agent-a"]},
        )
        meeting_id = mtg_resp.json()["data"]["id"]

        resp = client.post(
            f"/api/meetings/{meeting_id}/messages",
            json={
                "agent_id": "agent-a",
                "agent_name": "agent-a",
                "content": "My own message",
                "round_number": 1,
                "caller_agent_id": "agent-a",
            },
        )
        assert resp.status_code == 201
        assert "代打" not in resp.json()["message"]

        emitted = [call.args[0] for call in event_bus.emit.call_args_list]
        assert "meeting.impersonation" not in emitted
    finally:
        _teardown()


def test_legacy_empty_caller_no_audit():
    """caller_agent_id='' (old callers) → backward compatible, no audit."""
    client, repo, event_bus = _make_client()
    try:
        team_id = make_team({"name": "legacy-team", "mode": "coordinate"})["id"]

        mtg_resp = client.post(
            f"/api/teams/{team_id}/meetings",
            json={"topic": "legacy test", "participants": ["agent-a"]},
        )
        meeting_id = mtg_resp.json()["data"]["id"]

        resp = client.post(
            f"/api/meetings/{meeting_id}/messages",
            json={
                "agent_id": "agent-a",
                "agent_name": "agent-a",
                "content": "Legacy call without caller_agent_id",
                "round_number": 1,
            },
        )
        assert resp.status_code == 201
        emitted = [call.args[0] for call in event_bus.emit.call_args_list]
        assert "meeting.impersonation" not in emitted
    finally:
        _teardown()


# ============================================================
# HTTP: Conclude attendance validation
# ============================================================


def test_conclude_blocked_when_participant_missing():
    """2 expected, 1 spoken, validate_attendance=True, force=False → 400."""
    client, repo, event_bus = _make_client()
    try:
        team_id = make_team({"name": "attend-team", "mode": "coordinate"})["id"]

        mtg_resp = client.post(
            f"/api/teams/{team_id}/meetings",
            json={"topic": "attendance test", "participants": ["agent-a", "agent-b"]},
        )
        meeting_id = mtg_resp.json()["data"]["id"]

        # Only agent-a speaks
        client.post(
            f"/api/meetings/{meeting_id}/messages",
            json={
                "agent_id": "agent-a",
                "agent_name": "agent-a",
                "content": "agent-a says hi",
                "round_number": 1,
            },
        )

        resp = client.put(
            f"/api/meetings/{meeting_id}/conclude",
            json={"validate_attendance": True, "force": False},
        )
        assert resp.status_code == 400
        detail = resp.json()["detail"]
        assert "missing" in detail
        assert "agent-b" in detail["missing"]
        assert "force" in detail["hint"]
    finally:
        _teardown()


def test_conclude_force_succeeds_and_emits_warning():
    """force=True with missing participant → 200 + forced_conclude_with_missing event."""
    client, repo, event_bus = _make_client()
    try:
        team_id = make_team({"name": "force-team", "mode": "coordinate"})["id"]

        mtg_resp = client.post(
            f"/api/teams/{team_id}/meetings",
            json={"topic": "force test", "participants": ["agent-a", "agent-b"]},
        )
        meeting_id = mtg_resp.json()["data"]["id"]

        client.post(
            f"/api/meetings/{meeting_id}/messages",
            json={
                "agent_id": "agent-a",
                "agent_name": "agent-a",
                "content": "Only agent-a spoke",
                "round_number": 1,
            },
        )

        resp = client.put(
            f"/api/meetings/{meeting_id}/conclude",
            json={"validate_attendance": True, "force": True},
        )
        assert resp.status_code == 200
        assert resp.json()["success"] is True

        emitted = [call.args[0] for call in event_bus.emit.call_args_list]
        assert "meeting.forced_conclude_with_missing" in emitted

        # Verify event payload contains the missing agent
        for call in event_bus.emit.call_args_list:
            if call.args[0] == "meeting.forced_conclude_with_missing":
                event_data = call.args[2]
                assert "agent-b" in event_data["missing_participants"]
                break
    finally:
        _teardown()


def test_conclude_succeeds_when_all_spoke():
    """All expected participants spoke → 200, no warning event."""
    client, repo, event_bus = _make_client()
    try:
        team_id = make_team({"name": "full-team", "mode": "coordinate"})["id"]

        mtg_resp = client.post(
            f"/api/teams/{team_id}/meetings",
            json={"topic": "full attendance", "participants": ["agent-a", "agent-b"]},
        )
        meeting_id = mtg_resp.json()["data"]["id"]

        for agent in ["agent-a", "agent-b"]:
            client.post(
                f"/api/meetings/{meeting_id}/messages",
                json={
                    "agent_id": agent,
                    "agent_name": agent,
                    "content": f"{agent} speaks",
                    "round_number": 1,
                },
            )

        resp = client.put(
            f"/api/meetings/{meeting_id}/conclude",
            json={"validate_attendance": True, "force": False},
        )
        assert resp.status_code == 200
        emitted = [call.args[0] for call in event_bus.emit.call_args_list]
        assert "meeting.forced_conclude_with_missing" not in emitted
    finally:
        _teardown()


def test_conclude_does_not_write_memory():
    """结会**不再**自动写记忆层（2026-07-31 裁定）：结论走 decision 事件 + 任务墙。

    这曾是记忆层唯一的自动写入口——无人审、不挂失效轴管理、按会议数线性增长。
    """
    client, repo, _event_bus = _make_client()
    try:
        team_id = make_team({"name": "no-memory-team", "mode": "coordinate"})["id"]
        meeting_id = client.post(
            f"/api/teams/{team_id}/meetings",
            json={"topic": "无需入库的讨论", "participants": ["agent-a"]},
        ).json()["data"]["id"]
        client.post(
            f"/api/meetings/{meeting_id}/messages",
            json={
                "agent_id": "agent-a",
                "agent_name": "agent-a",
                "content": "结论内容",
                "round_number": 1,
            },
        )

        resp = client.put(
            f"/api/meetings/{meeting_id}/conclude",
            json={"summary": "本次结论", "validate_attendance": False},
        )
        assert resp.status_code == 200
        # 返回文案要把结论引向正道，而不是宣称"已保存到团队记忆"
        assert "任务墙" in resp.json()["message"]

        stored = asyncio.get_event_loop().run_until_complete(
            repo.list_memories("team", team_id)
        )
        assert stored == []
    finally:
        _teardown()


def test_conclude_skip_validation_when_disabled():
    """validate_attendance=False → conclude even with missing participants."""
    client, repo, event_bus = _make_client()
    try:
        team_id = make_team({"name": "skip-team", "mode": "coordinate"})["id"]

        mtg_resp = client.post(
            f"/api/teams/{team_id}/meetings",
            json={"topic": "skip attendance", "participants": ["agent-a", "agent-b"]},
        )
        meeting_id = mtg_resp.json()["data"]["id"]

        # No one speaks, but validation disabled
        resp = client.put(
            f"/api/meetings/{meeting_id}/conclude",
            json={"validate_attendance": False, "force": False},
        )
        assert resp.status_code == 200
    finally:
        _teardown()
