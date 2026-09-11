"""Automatic offline qualification and recovery cross real SQLite transactions."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta

import pytest
import pytest_asyncio
from sqlalchemy.sql.dml import Update
from sqlalchemy.sql.selectable import Select

from aiteam.api.event_bus import EventBus
from aiteam.api.hook_translator import HookTranslator
from aiteam.clock import utc_now
from aiteam.services.agent_liveness import (
    AUTO_OFFLINE_KEY,
    automatic_offline_at,
    source_activity_time,
)
from aiteam.storage import repository as repository_module
from aiteam.storage.connection import close_db
from aiteam.storage.repository import StorageRepository
from aiteam.types import HarnessId


def test_new_harness_requires_review_of_cc_specific_liveness():
    assert {harness for harness in HarnessId if harness != HarnessId.CLAUDE_CODE} == {HarnessId.CODEX}


@pytest_asyncio.fixture
async def state(tmp_path):
    url = f"sqlite+aiosqlite:///{tmp_path / 'liveness.db'}"
    repo, peer = StorageRepository(db_url=url), StorageRepository(db_url=url)
    await repo.init_db()
    team = await repo.create_team(name="native-test", mode="coordinate")
    agent = await repo.create_agent(
        team.id, "default", "default", source="hook", session_id="native-parent",
        cc_tool_use_id="native-child", config={"user_setting": {"keep": True}},
    )
    agent = await repo.update_agent(
        agent.id, status="busy", harness=HarnessId.CODEX,
        last_active_at=utc_now() - timedelta(minutes=10),
    )
    yield repo, peer, agent
    await close_db()


async def _offline(repo, agent):
    assert await repo.auto_offline_agent(agent, reason="heartbeat_timeout", occurred_at=utc_now())
    row = await repo.get_agent(agent.id)
    assert row.status == "offline"
    assert automatic_offline_at(row.config) is not None
    return row


async def _restore(repo, snapshot, **overrides):
    now = utc_now()
    arguments = {"native_id": "native-child", "session_id": "native-parent",
                 "source_time": now, "now": now, **overrides}
    return await repo.recover_codex_auto_offline(snapshot, **arguments)


async def test_automatic_reason_is_atomic_and_consumed_once(state):
    repo, peer, agent = state
    snapshot = await _offline(repo, agent)
    persisted = await peer.get_agent(agent.id)
    marker = persisted.config[AUTO_OFFLINE_KEY]
    assert marker["reason"] == "heartbeat_timeout"
    assert marker["version"] == 1
    assert len(marker["nonce"]) == 32
    assert persisted.config["user_setting"] == {"keep": True}
    assert (await _restore(peer, snapshot)).status == "busy"
    assert (await repo.get_agent(agent.id)).config == {"user_setting": {"keep": True}}
    assert await _restore(repo, snapshot) is None


async def test_manual_same_value_offline_revokes_qualification(state):
    repo, peer, agent = state
    snapshot = await _offline(repo, agent)
    await peer.update_agent(agent.id, status="offline")
    assert await _restore(repo, snapshot) is None
    row = await repo.get_agent(agent.id)
    assert row.status == "offline"
    assert row.config == {"user_setting": {"keep": True}}


async def test_old_reaper_snapshot_cannot_overwrite_new_activity(state):
    repo, peer, snapshot = state
    await peer.update_agent(snapshot.id, last_active_at=utc_now())
    assert not await repo.auto_offline_agent(
        snapshot, reason="heartbeat_timeout", occurred_at=utc_now(),
    )
    row = await repo.get_agent(snapshot.id)
    assert row.status == "busy"
    assert AUTO_OFFLINE_KEY not in row.config


async def test_config_liveness_cas_checks_current_harness(state):
    repo, peer, agent = state
    snapshot = await repo.update_agent(agent.id, harness=None)
    await peer.update_agent(agent.id, harness=HarnessId.CODEX)
    assert not await repo.auto_offline_agent(
        snapshot, reason="config_liveness", occurred_at=utc_now(),
    )
    assert not await repo.auto_offline_agent(
        await repo.get_agent(agent.id), reason="config_liveness", occurred_at=utc_now(),
    )
    assert (await repo.get_agent(agent.id)).status == "busy"


@pytest.mark.parametrize("changes", [
    {"native_id": "other-native"}, {"session_id": "other-session"},
])
async def test_recovery_requires_exact_dispatch_scope(state, changes):
    repo, _, agent = state
    snapshot = await _offline(repo, agent)
    assert await _restore(repo, snapshot, **changes) is None
    assert (await repo.get_agent(agent.id)).status == "offline"


async def test_team_end_revokes_even_if_team_later_reopens(state):
    repo, peer, agent = state
    snapshot = await _offline(repo, agent)
    await peer.update_team(agent.team_id, status="completed")
    assert await _restore(repo, snapshot) is None
    await peer.update_team(agent.team_id, status="active")
    assert await _restore(repo, snapshot) is None
    assert AUTO_OFFLINE_KEY not in (await repo.get_agent(agent.id)).config


async def test_rebound_agent_does_not_recover_from_prior_scope(state):
    repo, peer, agent = state
    snapshot = await _offline(repo, agent)
    other = await repo.create_team(name="other-native-team", mode="coordinate")
    await peer.update_agent(agent.id, team_id=other.id)
    assert await _restore(repo, snapshot) is None
    assert AUTO_OFFLINE_KEY not in (await repo.get_agent(agent.id)).config


async def test_later_auto_offline_nonce_invalidates_prior_recovery(state):
    repo, peer, agent = state
    first = await _offline(repo, agent)
    active = await _restore(repo, first)
    second = await _offline(peer, active)
    assert first.config[AUTO_OFFLINE_KEY]["nonce"] != second.config[AUTO_OFFLINE_KEY]["nonce"]
    assert await _restore(repo, first) is None
    assert (await repo.get_agent(agent.id)).status == "offline"


@pytest.mark.parametrize("first", ["manual", "recovery", "concurrent"])
async def test_manual_stop_wins_both_cas_orderings(state, first):
    repo, peer, agent = state
    snapshot = await _offline(repo, agent)
    if first == "manual":
        await peer.update_agent(agent.id, status="offline")
        assert await _restore(repo, snapshot) is None
    elif first == "recovery":
        assert await _restore(repo, snapshot) is not None
        await peer.update_agent(agent.id, status="offline")
    else:
        await asyncio.gather(_restore(repo, snapshot), peer.update_agent(agent.id, status="offline"))
    row = await repo.get_agent(agent.id)
    assert row.status == "offline"
    assert AUTO_OFFLINE_KEY not in row.config


async def test_subagent_stop_is_not_a_permanent_recovery_barrier(state):
    repo, _, agent = state
    await _offline(repo, agent)
    translator = HookTranslator(repo, EventBus(repo=repo))
    await translator.handle_event({
        "hook_event_name": "SubagentStop", "harness": "codex",
        "agent_id": "native-child", "session_id": "native-parent", "agent_type": "default",
    })
    stopped = await repo.get_agent(agent.id)
    assert stopped.status == "offline"
    assert AUTO_OFFLINE_KEY in stopped.config
    result = await translator.handle_event({
        "hook_event_name": "PreToolUse", "harness": "codex", "agent_id": "native-child",
        "session_id": "native-parent", "agent_type": "default", "tool_name": "Bash",
        "tool_input": {"command": "true"}, "source_observed_at": utc_now().isoformat(),
    })
    assert result["liveness_reason"] == "auto_offline_recovered"
    assert (await repo.get_agent(agent.id)).status == "busy"


async def test_legacy_waiting_self_heal_is_unchanged(state):
    repo, _, agent = state
    waiting = await repo.update_agent(agent.id, status="waiting", harness=None)
    translator = HookTranslator(repo, EventBus(repo=repo))
    assert await translator._self_heal_agent(waiting) is None
    assert (await repo.get_agent(agent.id)).status == "busy"


async def test_configuration_cannot_supply_recovery_qualification(state):
    repo, _, agent = state
    snapshot = await _offline(repo, agent)
    await repo.update_agent(agent.id, config=snapshot.config)
    assert AUTO_OFFLINE_KEY not in (await repo.get_agent(agent.id)).config
    assert await _restore(repo, snapshot) is None


@pytest.mark.parametrize("mutation", [
    {"version": True}, {"reason": []}, {"nonce": None}, {"occurred_at": "not-time"},
])
async def test_malformed_qualification_is_not_eligible(state, mutation):
    repo, _, agent = state
    snapshot = await _offline(repo, agent)
    snapshot.config[AUTO_OFFLINE_KEY].update(mutation)
    assert automatic_offline_at(snapshot.config) is None


@pytest.mark.parametrize("value,reason", [
    (None, "source_observed_at_missing"), ("bad", "source_observed_at_invalid"),
    ("2026-01-01T12:00:00", "source_observed_at_invalid"),
])
def test_source_time_never_falls_back_to_receive_time(value, reason):
    assert source_activity_time(value, utc_now()) == (None, reason)


@pytest.mark.parametrize("event", ["PreToolUse", "PostToolUse"])
@pytest.mark.parametrize("fault,reason", [
    ("session", "codex_scope_mismatch"), ("parent", "codex_scope_mismatch"),
    ("missing-time", "source_observed_at_missing"),
])
async def test_pipeline_rejects_unproven_recovery_with_fixed_diagnostic(state, event, fault, reason):
    repo, _, agent = state
    await _offline(repo, agent)
    translator = HookTranslator(repo, EventBus(repo=repo))
    payload = {
        "hook_event_name": event, "harness": "codex", "agent_id": "native-child",
        "agent_type": "default", "session_id": "native-parent", "tool_name": "Bash",
        "tool_input": {"command": "true"}, "source_observed_at": utc_now().isoformat(),
    }
    if fault == "session":
        payload["session_id"] = "other-parent"
    elif fault == "parent":
        payload["parent_thread_id"] = "other-parent"
    else:
        payload.pop("source_observed_at")
    result = await translator.handle_event(payload)
    assert result["liveness_reason"] == reason
    assert (await repo.get_agent(agent.id)).status == "offline"


async def test_observation_does_not_overwrite_known_conflicting_harness(state):
    repo, _, agent = state
    await _offline(repo, agent)
    await repo.update_agent(agent.id, harness=HarnessId.CLAUDE_CODE)
    translator = HookTranslator(repo, EventBus(repo=repo))
    result = await translator.handle_event({
        "hook_event_name": "PreToolUse", "harness": "codex", "agent_id": "native-child",
        "agent_type": "default", "session_id": "native-parent", "tool_name": "Bash",
        "tool_input": {}, "source_observed_at": utc_now().isoformat(),
    })
    assert result["liveness_reason"] == "codex_scope_mismatch"
    row = await repo.get_agent(agent.id)
    assert row.harness == HarnessId.CLAUDE_CODE
    assert row.status == "offline"


async def test_duplicate_start_does_not_bypass_manual_stop(state):
    repo, _, agent = state
    await _offline(repo, agent)
    await repo.update_agent(agent.id, status="offline")
    translator = HookTranslator(repo, EventBus(repo=repo))
    result = await translator.handle_event({
        "hook_event_name": "SubagentStart", "harness": "codex", "agent_id": "native-child",
        "agent_type": "default", "session_id": "native-parent",
        "source_observed_at": utc_now().isoformat(),
    })
    assert result["liveness_reason"] == "offline_requires_fresh_activity"
    assert (await repo.get_agent(agent.id)).status == "offline"


@pytest.mark.parametrize("interruption", ["automatic-offline", "automatic-recovery"])
async def test_manual_offline_uses_current_row_after_interleaved_transition(
    state, monkeypatch, interruption,
):
    repo, peer, agent = state
    snapshot = await _offline(repo, agent) if interruption == "automatic-recovery" else agent
    original_get_session = repository_module.get_session
    points = []
    armed = True

    async def interrupt(point):
        nonlocal armed
        armed = False
        points.append(point)
        if interruption == "automatic-offline":
            assert await peer.auto_offline_agent(
                snapshot, reason="heartbeat_timeout", occurred_at=utc_now(),
            )
        else:
            assert await _restore(peer, snapshot) is not None

    @asynccontextmanager
    async def interleaved_session(db_url=None):
        async with original_get_session(db_url) as session:
            execute = session.execute

            async def interleaved_execute(statement, *args, **kwargs):
                if armed and isinstance(statement, Update):
                    await interrupt("before_update")
                result = await execute(statement, *args, **kwargs)
                if armed and isinstance(statement, Select):
                    await interrupt("after_select")
                return result

            session.execute = interleaved_execute
            yield session

    monkeypatch.setattr(repository_module, "get_session", interleaved_session)
    returned = await repo.update_agent(agent.id, status="offline")
    assert len(points) == 1
    persisted = await peer.get_agent(agent.id)
    assert returned.status == persisted.status == "offline"
    assert AUTO_OFFLINE_KEY not in persisted.config
    assert await _restore(repo, persisted) is None


async def test_legacy_null_config_keeps_automatic_reaping(state):
    repo, _, agent = state
    snapshot = await repo.update_agent(agent.id, config=None)
    assert snapshot.config == {}
    assert await repo.auto_offline_agent(snapshot, reason="heartbeat_timeout", occurred_at=utc_now())
    assert (await repo.get_agent(agent.id)).status == "offline"
