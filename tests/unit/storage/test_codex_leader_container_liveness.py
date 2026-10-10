"""Owned Codex containers cross automatic close and native activity transactions."""
from datetime import timedelta
from uuid import uuid4

import pytest
import pytest_asyncio

from aiteam.api.event_bus import EventBus
from aiteam.api.hook_translator import HookTranslator
from aiteam.api.state_reaper import StateReaper
from aiteam.clock import utc_now
from aiteam.storage.connection import close_db
from aiteam.storage.repository import StorageRepository


@pytest_asyncio.fixture
async def container(tmp_path, monkeypatch):
    from pathlib import Path

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    # Rebased native Start now also launches the real capture subprocess.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / ".codex"))
    repo = StorageRepository(db_url=f"sqlite+aiosqlite:///{tmp_path / 'container.db'}")
    await repo.init_db()
    project = await repo.create_project(name="test", root_path=str(tmp_path))
    native = str(uuid4())
    team = await repo.create_team(
        f"session-{native}", "coordinate", config={"kind": "session", "owner_session_id": native},
        project_id=project.id,
    )
    agent = await repo.create_agent(
        team.id, "Codex Leader", "leader", source="hook", harness="codex", session_id=native,
        cc_tool_use_id=native, project_id=project.id,
    )
    await repo.update_team(team.id, leader_agent_id=agent.id, created_at=utc_now() - timedelta(hours=1))
    agent = await repo.update_agent(
        agent.id, status="busy", project_id=project.id, last_active_at=utc_now() - timedelta(hours=1),
    )
    assert await repo.auto_offline_agent(agent, reason="heartbeat_timeout", occurred_at=utc_now())
    bus = EventBus(repo)
    translator = HookTranslator(repo, bus)
    yield repo, translator, StateReaper(repo, bus), native, agent.id, team.id
    await translator.drain(timeout=10)
    await close_db()


def activity(native, **extra):
    return {
        "hook_event_name": "PreToolUse", "harness": "codex", "session_id": native,
        "agent_id": native, "tool_name": "Bash", "tool_input": {"command": "true"},
        "source_observed_at": utc_now().isoformat(), **extra,
    }


async def test_reaper_closed_container_recovers_original_leader_and_team(container):
    repo, translator, reaper, native, agent_id, team_id = container
    await reaper._check_stale_teams(utc_now(), repo)
    assert (await repo.get_team(team_id)).status == "completed"
    result = await translator.handle_event(activity(native))
    assert result["liveness_reason"] == "auto_offline_recovered"
    assert (await repo.get_agent(agent_id)).status == "busy"
    assert (await repo.get_team(team_id)).status == "active"


async def close(container):
    repo, _, reaper, _, _, team_id = container
    await reaper._check_stale_teams(utc_now(), repo)
    assert (await repo.get_team(team_id)).status == "completed"


async def restore(repo, agent, native, **extra):
    now = utc_now()
    return await repo.recover_codex_auto_offline(
        agent, native_id=native, session_id=native, source_time=now, now=now, **extra,
    )


async def test_original_rows_and_nonces_consumed_idempotently(container):
    from aiteam.services.agent_liveness import AUTO_CLOSE_KEY, AUTO_OFFLINE_KEY

    repo, translator, reaper, native, agent_id, team_id = container
    await close(container)
    before = await repo.get_agent(agent_id)
    before_team = await repo.get_team(team_id)
    assert before_team.config[AUTO_CLOSE_KEY]["offline_nonce"] == before.config[AUTO_OFFLINE_KEY]["nonce"]
    assert (await translator.handle_event(activity(native)))["liveness_reason"] == "auto_offline_recovered"
    assert await restore(repo, before, native) is None
    await reaper._check_stale_teams(utc_now(), repo)
    assert (await repo.get_agent(agent_id)).team_id == team_id
    assert AUTO_OFFLINE_KEY not in (await repo.get_agent(agent_id)).config
    assert AUTO_CLOSE_KEY not in (await repo.get_team(team_id)).config
    assert (await repo.get_team(team_id)).status == "active"


@pytest.mark.parametrize("fault", ["missing", "stale", "future", "naive", "invalid", "before-close", "before-offline"])
async def test_source_clock_never_guesses_live_activity(container, fault):
    from aiteam.services.agent_liveness import AUTO_CLOSE_KEY, AUTO_OFFLINE_KEY

    repo, translator, _, native, agent_id, team_id = container
    await close(container)
    packet = activity(native)
    values = {
        "stale": (utc_now() - timedelta(minutes=5)).isoformat(),
        "future": (utc_now() + timedelta(minutes=5)).isoformat(),
        "naive": utc_now().replace(tzinfo=None).isoformat(), "invalid": "bad",
        "before-close": (await repo.get_team(team_id)).config[AUTO_CLOSE_KEY]["occurred_at"],
        "before-offline": (await repo.get_agent(agent_id)).config[AUTO_OFFLINE_KEY]["occurred_at"],
    }
    if fault == "missing":
        packet.pop("source_observed_at")
    else:
        packet["source_observed_at"] = values[fault]
    result = await translator.handle_event(packet)
    assert result["liveness_reason"] != "auto_offline_recovered"
    assert (await repo.get_agent(agent_id)).status == "offline"
    assert (await repo.get_team(team_id)).status == "completed"


@pytest.mark.parametrize("marker", [{"origin_at": "bad"}, None, "bad", {"origin_at": "2020-01-01T00:00:00Z"}])
async def test_real_entry_replay_marker_never_recovers(container, marker):
    repo, translator, reaper, native, agent_id, team_id = container
    await close(container)
    before = await repo.get_agent(agent_id)
    await translator.handle_event(activity(native, _hook_replay=marker))
    assert (await repo.get_agent(agent_id)).last_active_at == before.last_active_at
    assert (await repo.get_agent(agent_id)).status == "offline"
    await reaper._check_stale_teams(utc_now(), repo)
    assert (await repo.get_team(team_id)).status == "completed"


@pytest.mark.parametrize("target,changes", [
    ("agent", {"status": "offline"}), ("agent", {"config": {"keep": True}}),
    ("agent", {"role": "member"}), ("agent", {"project_id": "other-project"}),
    ("agent", {"session_id": "wrong-session"}), ("agent", {"cc_tool_use_id": "wrong-native"}),
    ("agent", {"source": "api"}), ("agent", {"harness": "claude-code"}),
    ("team", {"status": "completed"}), ("team", {"config": {"kind": "session", "owner_session_id": "wrong"}}),
    ("team", {"project_id": "other-project"}), ("team", {"leader_agent_id": None}),
    ("team", {"name": "wrong-name"}),
])
async def test_manual_writes_revoke_even_with_old_snapshot(container, target, changes):
    from aiteam.services.agent_liveness import AUTO_OFFLINE_KEY

    repo, _, reaper, native, agent_id, team_id = container
    await close(container)
    before = await repo.get_agent(agent_id)
    if "project_id" in changes:
        project = await repo.create_project(name="other-project", root_path="/other-test-root")
        changes = {**changes, "project_id": project.id}
    await (repo.update_agent(agent_id, **changes) if target == "agent" else repo.update_team(team_id, **changes))
    assert await restore(repo, before, native) is None
    assert AUTO_OFFLINE_KEY not in (await repo.get_agent(agent_id)).config
    await reaper._check_stale_teams(utc_now(), repo)
    assert (await repo.get_team(team_id)).status == "completed"


@pytest.mark.parametrize("order", ["manual-first", "recovery-first", "concurrent"])
async def test_manual_team_close_wins_both_orderings(container, order):
    import asyncio

    repo, _, reaper, native, agent_id, team_id = container
    await close(container)
    before = await repo.get_agent(agent_id)
    if order == "manual-first":
        await repo.update_team(team_id, status="completed")
        assert await restore(repo, before, native) is None
    elif order == "recovery-first":
        assert await restore(repo, before, native) is not None
        await repo.update_team(team_id, status="completed")
    else:
        await asyncio.gather(restore(repo, before, native), repo.update_team(team_id, status="completed"))
    await reaper._check_stale_teams(utc_now(), repo)
    assert (await repo.get_team(team_id)).status == "completed"


async def test_busy_child_has_no_authority_to_open_parent(container):
    repo, translator, reaper, native, agent_id, team_id = container
    child_native = str(uuid4())
    child = await repo.create_agent(team_id, "child", "member", harness="codex", source="hook",
                                    cc_tool_use_id=child_native, session_id=native)
    child = await repo.update_agent(child.id, status="waiting", last_active_at=utc_now() - timedelta(hours=1))
    await close(container)
    result = await translator.handle_event(activity(native, agent_id=child_native))
    assert result["liveness_reason"] != "auto_offline_recovered"
    await repo.update_agent(child.id, status="busy")
    await reaper._check_stale_teams(utc_now(), repo)
    assert (await repo.get_team(team_id)).status == "completed"
    assert (await repo.get_agent(agent_id)).status == "offline"


async def test_cc_contradictory_busy_team_still_reopens(container):
    repo, _, reaper, _, agent_id, team_id = container
    await repo.update_agent(agent_id, harness="claude-code", status="busy")
    await repo.update_team(team_id, status="completed")
    await reaper._check_stale_teams(utc_now(), repo)
    assert (await repo.get_team(team_id)).status == "active"


@pytest.mark.parametrize("fault", [
    "activity", "new-member", "member-move", "team-config", "team-time", "leader-role", "leader-project",
])
async def test_old_close_snapshot_cannot_close_changed_roster(container, fault):
    repo, _, _, _, agent_id, team_id = container
    team, members = await repo.get_team(team_id), await repo.list_agents(team_id)
    if fault == "activity":
        await repo.update_agent(agent_id, last_active_at=utc_now())
    elif fault == "new-member":
        await repo.create_agent(team_id, "new-member", "member")
    elif fault == "member-move":
        other = await repo.create_team("other-team", "coordinate")
        await repo.update_agent(agent_id, team_id=other.id)
    elif fault == "team-config":
        await repo.update_team(team_id, config={**team.config, "setting": "changed"})
    elif fault == "team-time":
        await repo.update_team(team_id, updated_at=utc_now())
    elif fault == "leader-role":
        await repo.update_agent(agent_id, role="member")
    else:
        project = await repo.create_project(name="other-project", root_path="/another-test")
        await repo.update_agent(agent_id, project_id=project.id)
    assert not await repo.auto_close_codex_session(team, members, now=utc_now())
    assert (await repo.get_team(team_id)).status == "active"


async def test_reaper_conflict_does_not_fall_back_to_general_writes(container, monkeypatch):
    repo, _, reaper, _, agent_id, team_id = container
    original = repo.auto_close_codex_session

    async def race(team, members, *, now):
        await repo.update_agent(agent_id, last_active_at=now)
        return await original(team, members, now=now)

    monkeypatch.setattr(repo, "auto_close_codex_session", race)
    await reaper._check_stale_teams(utc_now(), repo)
    assert (await repo.get_team(team_id)).status == "active"
    assert (await repo.get_agent(agent_id)).last_active_at > utc_now() - timedelta(seconds=5)


@pytest.mark.parametrize("fault", ["role", "project", "owner", "nonce", "leader-pointer", "native"])
async def test_recovery_rechecks_identity_inside_sql(container, monkeypatch, fault):
    from contextlib import asynccontextmanager

    from sqlalchemy import update
    from sqlalchemy.sql.dml import Update

    from aiteam.storage import repository as module
    from aiteam.storage.models import AgentModel, TeamModel

    repo, _, _, native, agent_id, team_id = container
    await close(container)
    before = await repo.get_agent(agent_id)
    peer = StorageRepository(db_url=repo._db_url)
    original = module.get_session
    armed = True

    @asynccontextmanager
    async def interleave(db_url=None):
        nonlocal armed
        async with original(db_url) as session:
            execute = session.execute

            async def run(statement, *args, **kwargs):
                nonlocal armed
                if armed and isinstance(statement, Update):
                    armed = False
                    # Simulate an out-of-band concurrent identity edit that
                    # retains the marker: SQL must not rely on the Python check.
                    async with original(repo._db_url) as writer:
                        if fault in {"role", "project", "native"}:
                            changes = {"role": "member"} if fault == "role" else (
                                {"project_id": None} if fault == "project" else {"cc_tool_use_id": str(uuid4())}
                            )
                            await writer.execute(update(AgentModel).where(AgentModel.id == agent_id).values(**changes))
                        elif fault == "leader-pointer":
                            await writer.execute(
                                update(TeamModel).where(TeamModel.id == team_id).values(leader_agent_id=None),
                            )
                        else:
                            team = await peer.get_team(team_id)
                            config = {**team.config}
                            if fault == "owner":
                                config["owner_session_id"] = str(uuid4())
                            else:
                                from aiteam.services.agent_liveness import AUTO_CLOSE_KEY
                                config[AUTO_CLOSE_KEY] = {**config[AUTO_CLOSE_KEY], "nonce": uuid4().hex}
                            await writer.execute(update(TeamModel).where(TeamModel.id == team_id).values(config=config))
                return await execute(statement, *args, **kwargs)
            session.execute = run
            yield session

    monkeypatch.setattr(module, "get_session", interleave)
    assert await restore(repo, before, native) is None
    assert (await peer.get_agent(agent_id)).status == "offline"
    assert (await peer.get_team(team_id)).status == "completed"


async def test_second_row_failure_rolls_back_first_row(container, monkeypatch):
    from contextlib import asynccontextmanager

    from sqlalchemy import update
    from sqlalchemy.sql.dml import Update

    from aiteam.storage import repository as module
    from aiteam.storage.models import TeamModel

    repo, _, _, native, agent_id, team_id = container
    await close(container)
    before = await repo.get_agent(agent_id)
    original = module.get_session

    @asynccontextmanager
    async def fail_team_update(db_url=None):
        async with original(db_url) as session:
            execute = session.execute

            async def run(statement, *args, **kwargs):
                if isinstance(statement, Update) and statement.table.name == "teams":
                    statement = (update(TeamModel).where(TeamModel.id == "does-not-exist")
                                 .values(status="active").returning(TeamModel.id))
                return await execute(statement, *args, **kwargs)
            session.execute = run
            yield session

    monkeypatch.setattr(module, "get_session", fail_team_update)
    assert await restore(repo, before, native) is None
    assert (await repo.get_agent(agent_id)).status == "offline"
    assert (await repo.get_team(team_id)).status == "completed"


@pytest.mark.parametrize("state", ["stale", "completed-busy"])
async def test_cc_owner_with_codex_child_preserves_cc_lifecycle(container, state):
    repo, _, reaper, _, agent_id, team_id = container
    await repo.update_agent(agent_id, harness="claude-code")
    child = await repo.create_agent(team_id, "codex-child", "member", harness="codex", source="hook",
                                    session_id="child-parent", cc_tool_use_id="child-native")
    await repo.update_agent(child.id, status="waiting", last_active_at=utc_now() - timedelta(hours=1))
    if state == "completed-busy":
        await repo.update_agent(agent_id, status="busy")
        await repo.update_team(team_id, status="completed")
    await reaper._check_stale_teams(utc_now(), repo)
    assert (await repo.get_team(team_id)).status == ("completed" if state == "stale" else "active")


@pytest.mark.parametrize("change", [{"status": "active"}, {"name": "cc-renamed"}, {"config": {"keep": True}}])
async def test_active_cc_metadata_does_not_revoke_codex_child_qualification(container, change):
    repo, _, _, _, agent_id, team_id = container
    await repo.update_agent(agent_id, harness="claude-code")
    child = await repo.create_agent(team_id, "codex-child", "member", harness="codex", source="hook",
                                    session_id="child-parent", cc_tool_use_id="child-native")
    child = await repo.update_agent(child.id, status="busy", last_active_at=utc_now() - timedelta(hours=1))
    assert await repo.auto_offline_agent(child, reason="heartbeat_timeout", occurred_at=utc_now())
    before = await repo.get_agent(child.id)
    await repo.update_team(team_id, **change)
    now = utc_now()
    assert await repo.recover_codex_auto_offline(before, native_id="child-native", session_id="child-parent",
                                               source_time=now, now=now) is not None


async def test_active_unbound_leader_preserves_existing_recovery_contract(container):
    repo, _, _, native, agent_id, team_id = container
    cleared = await repo.update_team(team_id, name="unbound-team", project_id=None, config=None)
    assert cleared.config == {}
    from sqlalchemy import text

    from aiteam.storage.connection import get_session
    async with get_session(repo._db_url) as session:
        assert (await session.execute(
            text("SELECT json_type(config) FROM teams WHERE id=:id"), {"id": team_id},
        )).scalar() == "null"
    agent = await repo.update_agent(agent_id, project_id=None, status="busy")
    assert await repo.auto_offline_agent(agent, reason="heartbeat_timeout", occurred_at=utc_now())
    before = await repo.get_agent(agent_id)
    assert await restore(repo, before, native) is not None
    assert (await repo.get_team(team_id)).status == "active"


def observation(native, *, child=None):
    return {
        "version": 1, "actor_id": child or native, "root_session_id": native,
        "parent_thread_id": native if child else None, "source_kind": "subagent" if child else "cli",
        "agent_name": "child" if child else "", "agent_role": "member" if child else "leader",
        "model": "", "transcript_path": "", "cli_version": "",
    }


async def test_child_start_keeps_old_offline_behavior(container):
    repo, translator, _, native, _, team_id = container
    child_native = str(uuid4())
    child = await repo.create_agent(team_id, "child", "member", harness="codex", source="hook",
                                    session_id=native, cc_tool_use_id=child_native)
    child = await repo.update_agent(child.id, status="busy")
    assert await repo.auto_offline_agent(child, reason="heartbeat_timeout", occurred_at=utc_now())
    await translator.handle_event(activity(native, agent_id=child_native, hook_event_name="SubagentStart",
                                           codex_observation=observation(native, child=child_native)))
    assert (await repo.get_agent(child.id)).status == "offline"


async def test_true_native_session_end_revokes_closed_owner_eligibility(container):
    repo, translator, reaper, native, agent_id, team_id = container
    await close(container)
    await translator.handle_event(activity(native, hook_event_name="SessionEnd",
                                           codex_observation=observation(native)))
    assert (await translator.handle_event(activity(native)))["liveness_reason"] == "auto_offline_not_eligible"
    await reaper._check_stale_teams(utc_now(), repo)
    assert (await repo.get_agent(agent_id)).status == "offline"
    assert (await repo.get_team(team_id)).status == "completed"


async def test_active_root_offline_recovery_emits_no_team_revived_event(container):
    repo, translator, _, native, _, _ = container
    assert (await translator.handle_event(activity(native)))["liveness_reason"] == "auto_offline_recovered"
    assert await repo.list_events(event_type="team.auto_revived") == []


async def test_reserved_team_marker_cannot_be_injected(container):
    from aiteam.services.agent_liveness import AUTO_CLOSE_KEY

    repo, _, _, _, agent_id, team_id = container
    await close(container)
    team = await repo.get_team(team_id)
    await repo.update_team(team_id, config=team.config)
    assert AUTO_CLOSE_KEY not in (await repo.get_team(team_id)).config
    created = await repo.create_team("injected-team", "coordinate", config=team.config)
    assert AUTO_CLOSE_KEY not in created.config


@pytest.mark.parametrize("owner", ["exact", "prefix-collision"])
async def test_legacy_short_name_requires_full_owner_identity(container, owner):
    repo, translator, reaper, native, agent_id, team_id = container
    team = await repo.get_team(team_id)
    config = {**team.config}
    if owner == "prefix-collision":
        config["owner_session_id"] = native[:8] + str(uuid4())[8:]
    await repo.update_team(team_id, name=f"session-{native[:8]}", config=config)
    agent = await repo.update_agent(agent_id, status="busy")
    assert await repo.auto_offline_agent(agent, reason="heartbeat_timeout", occurred_at=utc_now())
    await reaper._check_stale_teams(utc_now(), repo)
    result = await translator.handle_event(activity(native))
    assert (result["liveness_reason"] == "auto_offline_recovered") == (owner == "exact")
    assert (await repo.get_team(team_id)).status == ("active" if owner == "exact" else "completed")


@pytest.mark.parametrize("fault", ["session", "native", "parent"])
async def test_closed_root_rejects_wrong_dispatch_scope(container, fault):
    repo, translator, _, native, agent_id, team_id = container
    await close(container)
    packet = activity(native)
    packet[{"session": "session_id", "native": "agent_id", "parent": "parent_thread_id"}[fault]] = str(uuid4())
    await translator.handle_event(packet)
    assert (await repo.get_agent(agent_id)).status == "offline"
    assert (await repo.get_team(team_id)).status == "completed"


async def test_root_native_start_can_restore_its_auto_closed_container(container):
    repo, translator, _, native, agent_id, team_id = container
    await close(container)
    result = await translator.handle_event(activity(native, agent_id=None, hook_event_name="SessionStart",
                                                    codex_observation=observation(native)))
    assert result["liveness_reason"] == "auto_offline_recovered"
    assert (await repo.get_agent(agent_id)).status == "busy"
    assert (await repo.get_team(team_id)).status == "active"


async def test_active_recovery_source_must_follow_current_offline_nonce(container):
    from aiteam.services.agent_liveness import automatic_offline_at

    repo, _, _, native, agent_id, _ = container
    before = await repo.get_agent(agent_id)
    source_time = automatic_offline_at(before.config)
    assert await repo.recover_codex_auto_offline(
        before, native_id=native, session_id=native, source_time=source_time, now=utc_now(),
    ) is None


async def test_completion_outbox_replay_is_non_live_even_with_fresh_source_time(container):
    repo, translator, _, native, agent_id, team_id = container
    await close(container)
    before = await repo.get_agent(agent_id)
    result = await translator.handle_event(activity(
        native, hook_event_name="PostToolUse", _codex_completion_replay=True,
        _codex_completion_observed_at=utc_now().isoformat(), tool_call_id="replayed-completion",
    ))
    assert result["completion_recorded"] is True
    activities = await repo.list_activities(agent_id=agent_id)
    assert len(activities) == 1 and activities[0].id == result["activity_id"]
    assert activities[0].output_summary == ""
    assert (await repo.get_agent(agent_id)).status == "offline"
    assert (await repo.get_agent(agent_id)).last_active_at == before.last_active_at
    assert (await repo.get_team(team_id)).status == "completed"


async def test_stale_source_after_old_offline_and_close_still_cannot_recover(container):
    repo, translator, _, native, agent_id, team_id = container
    agent = await repo.update_agent(agent_id, status="busy", last_active_at=utc_now() - timedelta(hours=2))
    assert await repo.auto_offline_agent(agent, reason="heartbeat_timeout", occurred_at=utc_now() - timedelta(hours=1))
    assert await repo.auto_close_codex_session(await repo.get_team(team_id), await repo.list_agents(team_id),
                                               now=utc_now() - timedelta(minutes=31))
    result = await translator.handle_event(
        activity(native, source_observed_at=(utc_now() - timedelta(minutes=2)).isoformat()),
    )
    assert result["liveness_reason"] == "source_observed_at_stale"
    assert (await repo.get_agent(agent_id)).status == "offline"


async def test_reopen_has_one_audit_event_and_agent_broadcast_duplicates_add_none(container, monkeypatch):
    from aiteam.api import event_bus as module

    repo, translator, _, native, agent_id, _ = container
    await close(container)
    before = await repo.get_agent(agent_id)
    broadcasts = []

    async def capture(event):
        broadcasts.append(event)

    monkeypatch.setattr(module.ws_manager, "broadcast_event", capture)
    assert (await translator.handle_event(activity(native)))["liveness_reason"] == "auto_offline_recovered"
    assert len(await repo.list_events(event_type="team.auto_revived")) == 1
    status_events = [event for event in broadcasts if event.event_type == "agent.status_changed"]
    assert len(status_events) == 1
    assert status_events[0].data["agent_id"] == agent_id
    assert status_events[0].data["status"] == "busy"
    assert await restore(repo, before, native) is None
    await translator.handle_event(activity(native))
    await translator.handle_event(activity(native, _hook_replay=None))
    await repo.update_agent(agent_id, status="offline")
    assert (await translator.handle_event(activity(native)))["liveness_reason"] == "auto_offline_not_eligible"
    assert len(await repo.list_events(event_type="team.auto_revived")) == 1
    assert len([event for event in broadcasts if event.event_type == "agent.status_changed"]) == 1


@pytest.mark.parametrize("status,age", [("busy", 45), ("waiting", 120), ("busy", 120)])
async def test_startup_waiting_paths_retain_real_automatic_recovery(container, status, age):
    from aiteam.api.deps import _startup_reconciliation

    repo, translator, reaper, native, agent_id, team_id = container
    await repo.update_agent(agent_id, status=status, last_active_at=utc_now() - timedelta(minutes=age))
    await _startup_reconciliation(repo)
    assert (await repo.get_agent(agent_id)).status == ("waiting" if age == 45 else "offline")
    await reaper._check_stale_teams(utc_now(), repo)
    assert (await repo.get_team(team_id)).status == "completed"
    assert (await translator.handle_event(activity(native)))["liveness_reason"] == "auto_offline_recovered"
    assert (await repo.get_agent(agent_id)).status == "busy"
    assert (await repo.get_team(team_id)).status == "active"


async def test_exact_manual_busy_owner_can_correct_completed_container(container):
    repo, _, reaper, _, agent_id, team_id = container
    await close(container)
    await repo.update_agent(agent_id, status="busy")
    await reaper._check_stale_teams(utc_now(), repo)
    assert (await repo.get_team(team_id)).status == "active"
    assert (await repo.get_agent(agent_id)).status == "busy"


@pytest.mark.parametrize("metadata", ["none", "owner", "pointer"])
async def test_explicit_close_normalizes_previous_exact_owner_before_rebinding(container, metadata):
    repo, _, reaper, _, agent_id, team_id = container
    await repo.update_agent(agent_id, status="busy")
    team = await repo.get_team(team_id)
    changes = {"config": None} if metadata == "none" else (
        {"config": {**team.config, "owner_session_id": str(uuid4())}}
        if metadata == "owner" else {"leader_agent_id": None}
    )
    await repo.update_team(team_id, status="completed", **changes)
    assert (await repo.get_agent(agent_id)).status == "offline"
    assert (await repo.get_agent(agent_id)).current_task is None
    assert (await repo.get_team(team_id)).status == "completed"
    await reaper._check_stale_teams(utc_now(), repo)
    assert (await repo.get_team(team_id)).status == "completed"


async def test_root_parent_cannot_equal_its_own_session(container):
    repo, translator, _, native, agent_id, _ = container
    result = await translator.handle_event(activity(native, parent_thread_id=native))
    assert result["liveness_reason"] == "codex_scope_mismatch"
    assert (await repo.get_agent(agent_id)).status == "offline"


async def test_48_real_handle_events_restore_and_audit_exactly_once(container):
    import asyncio

    repo, translator, _, native, agent_id, team_id = container
    await close(container)
    gate = asyncio.Event()

    async def call():
        await gate.wait()
        return await translator.handle_event(activity(native))

    calls = [asyncio.create_task(call()) for _ in range(48)]
    gate.set()
    results = await asyncio.gather(*calls)
    assert sum(result.get("liveness_reason") == "auto_offline_recovered" for result in results) == 1
    assert len(await repo.list_events(event_type="team.auto_revived")) == 1
    assert (await repo.get_agent(agent_id)).status == "busy"
    assert (await repo.get_team(team_id)).status == "active"
    assert len(await repo.list_agents(team_id)) == 1


@pytest.mark.parametrize("first", ["close", "busy", "concurrent"])
async def test_explicit_busy_and_close_order_normalizes_both_states(container, first):
    import asyncio

    repo, _, reaper, _, agent_id, team_id = container
    await close(container)
    if first == "close":
        await repo.update_team(team_id, status="completed")
        await repo.update_agent(agent_id, status="busy")
        await reaper._check_stale_teams(utc_now(), repo)
        assert (await repo.get_team(team_id)).status == "active"
    elif first == "busy":
        await repo.update_agent(agent_id, status="busy")
        await repo.update_team(team_id, status="completed")
        await reaper._check_stale_teams(utc_now(), repo)
        assert (await repo.get_agent(agent_id)).status == "offline"
        assert (await repo.get_team(team_id)).status == "completed"
    else:
        await asyncio.gather(repo.update_agent(agent_id, status="busy"), repo.update_team(team_id, status="completed"))
        root = await repo.get_agent(agent_id)
        await reaper._check_stale_teams(utc_now(), repo)
        assert (await repo.get_team(team_id)).status == ("active" if root.status == "busy" else "completed")


async def test_old_busy_owner_snapshot_loses_to_new_explicit_close(container):
    repo, _, _, _, agent_id, team_id = container
    await close(container)
    busy = await repo.update_agent(agent_id, status="busy")
    team = await repo.get_team(team_id)
    await repo.update_team(team_id, status="completed")
    assert not await repo.revive_codex_busy_owner(team, busy, now=utc_now())
    assert (await repo.get_agent(agent_id)).status == "offline"
    assert (await repo.get_team(team_id)).status == "completed"


async def test_automatic_close_audit_distinguishes_unknown_offline(container, caplog):
    from aiteam.services.agent_liveness import AUTO_OFFLINE_KEY

    repo, _, reaper, _, agent_id, team_id = container
    await repo.update_agent(agent_id, status="offline")
    await reaper._check_stale_teams(utc_now(), repo)
    records = await repo.list_events(event_type="agent.updated", entity_id=agent_id)
    audit = next(record for record in records if record.data.get("reason") == "codex_session_auto_close")
    assert audit.data["previous_status"] == "offline"
    assert audit.data["recovery_eligible"] is False
    assert audit.data["close_marker"] is None
    assert AUTO_OFFLINE_KEY not in (await repo.get_agent(agent_id)).config
    assert (await repo.get_team(team_id)).status == "completed"


async def test_startup_old_snapshot_cannot_undo_manual_offline(container):
    repo, _, _, _, agent_id, team_id = container
    busy = await repo.update_agent(agent_id, status="busy")
    team = await repo.get_team(team_id)
    await repo.update_agent(agent_id, status="offline")
    assert await repo.startup_reconcile_codex_owner(busy, team, status="waiting", now=utc_now()) is None
    assert (await repo.get_agent(agent_id)).status == "offline"


async def test_full_cycle_startup_then_owner_busy_then_close_stays_recoverable(container):
    from aiteam.api.deps import _startup_reconciliation
    from aiteam.services.agent_liveness import AUTO_OFFLINE_KEY

    repo, translator, reaper, native, agent_id, team_id = container
    await repo.update_agent(agent_id, status="busy", last_active_at=utc_now() - timedelta(minutes=45))
    await _startup_reconciliation(repo)
    await reaper._reap_cycle_for_repo(repo)
    assert (await repo.get_team(team_id)).status == "completed"
    assert AUTO_OFFLINE_KEY in (await repo.get_agent(agent_id)).config
    assert (await translator.handle_event(activity(native)))["liveness_reason"] == "auto_offline_recovered"
    await repo.update_team(team_id, status="completed")
    assert (await repo.get_agent(agent_id)).status == "offline"
    await repo.update_agent(agent_id, status="busy")
    await reaper._reap_cycle_for_repo(repo)
    assert (await repo.get_team(team_id)).status == "active"
    await repo.update_agent(agent_id, last_active_at=utc_now() - timedelta(minutes=6))
    await reaper._reap_cycle_for_repo(repo)
    assert (await repo.get_agent(agent_id)).status == "offline"
    assert AUTO_OFFLINE_KEY in (await repo.get_agent(agent_id)).config


async def test_active_child_recovery_tolerates_unrelated_team_write(container, monkeypatch):
    from contextlib import asynccontextmanager

    from sqlalchemy.sql.dml import Update

    from aiteam.storage import repository as module

    repo, _, _, native, _, team_id = container
    child_id = str(uuid4())
    child = await repo.create_agent(team_id, "child", "member", harness="codex", source="hook",
                                    session_id=native, cc_tool_use_id=child_id)
    child = await repo.update_agent(child.id, status="busy")
    assert await repo.auto_offline_agent(child, reason="heartbeat_timeout", occurred_at=utc_now())
    child = await repo.get_agent(child.id)
    original = module.get_session
    peer = StorageRepository(db_url=repo._db_url)
    armed = True

    @asynccontextmanager
    async def changed(db_url=None):
        nonlocal armed
        async with original(db_url) as session:
            execute = session.execute

            async def run(statement, *args, **kwargs):
                nonlocal armed
                if armed and isinstance(statement, Update):
                    armed = False
                    await peer.update_team(team_id, summary="unrelated metadata")
                return await execute(statement, *args, **kwargs)
            session.execute = run
            yield session

    monkeypatch.setattr(module, "get_session", changed)
    now = utc_now()
    assert await repo.recover_codex_auto_offline(child, native_id=child_id, session_id=native,
                                                source_time=now, now=now) is not None


async def test_current_sql_close_handles_concurrent_owner_switch(container, monkeypatch):
    from contextlib import asynccontextmanager

    from sqlalchemy.sql.dml import Update

    from aiteam.storage import repository as module

    repo, _, _, _, old_id, team_id = container
    await repo.update_agent(old_id, status="busy")
    native = str(uuid4())
    other = await repo.create_agent(team_id, "other", "leader", source="hook", harness="codex",
                                    session_id=native, cc_tool_use_id=native)
    team = await repo.get_team(team_id)
    await repo.update_agent(other.id, project_id=team.project_id, status="busy")
    original = module.get_session
    peer = StorageRepository(db_url=repo._db_url)
    armed = True

    @asynccontextmanager
    async def changed(db_url=None):
        nonlocal armed
        async with original(db_url) as session:
            execute = session.execute

            async def run(statement, *args, **kwargs):
                nonlocal armed
                if armed and isinstance(statement, Update):
                    armed = False
                    await peer.update_team(team_id, name=f"session-{native}", leader_agent_id=other.id,
                                           config={"kind": "session", "owner_session_id": native})
                return await execute(statement, *args, **kwargs)
            session.execute = run
            yield session

    monkeypatch.setattr(module, "get_session", changed)
    await repo.update_team(team_id, status="completed")
    assert (await repo.get_agent(other.id)).status == "offline"
    assert (await repo.get_agent(old_id)).status == "busy"
    assert (await repo.get_team(team_id)).leader_agent_id == other.id


@pytest.mark.parametrize("removed", ["pointer", "config"])
async def test_manual_close_identity_removal_does_not_let_busy_child_open(container, removed):
    repo, _, reaper, native, agent_id, team_id = container
    child = await repo.create_agent(team_id, "child", "member", harness="codex", source="hook",
                                    session_id=native, cc_tool_use_id=str(uuid4()))
    await repo.update_agent(agent_id, status="busy")
    await repo.update_agent(child.id, status="busy")
    await repo.update_team(team_id, status="completed", **({"leader_agent_id": None}
                                                          if removed == "pointer" else {"config": None}))
    assert (await repo.get_agent(agent_id)).status == "offline"
    await reaper._check_stale_teams(utc_now(), repo)
    assert (await repo.get_team(team_id)).status == "completed"


async def test_actual_cc_birth_contract_keeps_legacy_fallback_with_codex_children(container):
    repo, translator, reaper, _, _, original_team_id = container
    project = await repo.get_project((await repo.get_team(original_team_id)).project_id)
    native = str(uuid4())
    # The actual CC birth function, not a renamed Codex fixture.
    cc = await translator._birth_session_leader(native, project, {"session_id": native, "cwd": project.root_path})
    assert cc.cc_tool_use_id is None
    team = await repo.get_team(cc.team_id)
    assert team.project_id is None
    child = await repo.create_agent(team.id, "child", "member", source="hook", harness="codex",
                                    session_id=native, cc_tool_use_id=str(uuid4()))
    await repo.update_agent(child.id, status="busy")
    await repo.update_team(team.id, status="completed")
    await reaper._check_stale_teams(utc_now(), repo)
    assert (await repo.get_team(team.id)).status == "active"


async def test_true_cc_owner_rebinding_takes_priority_over_residual_codex_root(container):
    repo, translator, reaper, _, old_id, team_id = container
    project = await repo.get_project((await repo.get_team(team_id)).project_id)
    cc_native = str(uuid4())
    cc = await translator._birth_session_leader(cc_native, project, {"session_id": cc_native, "cwd": project.root_path})
    await repo.update_team(cc.team_id, name="previous-cc-container")
    await repo.update_agent(cc.id, team_id=team_id, status="busy")
    # This residual root really matches the new name/project/native evidence;
    # only a currently valid CC owner must suppress the Codex classification.
    residual = await repo.create_agent(team_id, "residual", "leader", source="hook", harness="codex",
                                      session_id=cc_native, cc_tool_use_id=cc_native)
    await repo.update_agent(residual.id, project_id=project.id, status="busy")
    await repo.update_team(team_id, status="completed", name=f"session-{cc_native[:8]}",
                           leader_agent_id=cc.id, config={"kind": "session", "owner_session_id": cc_native})
    assert (await repo.get_agent(old_id)).status == "offline"
    await reaper._check_stale_teams(utc_now(), repo)
    assert (await repo.get_team(team_id)).status == "active"


@pytest.mark.parametrize("changed", ["name", "project"])
async def test_manual_close_metadata_mismatch_cannot_let_busy_child_open(container, changed):
    repo, _, reaper, native, agent_id, team_id = container
    child = await repo.create_agent(team_id, "child", "member", harness="codex", source="hook",
                                    session_id=native, cc_tool_use_id=str(uuid4()))
    await repo.update_agent(child.id, status="busy")
    await repo.update_agent(agent_id, status="busy")
    changes = {"name": "renamed-container"} if changed == "name" else {"project_id": None}
    await repo.update_team(team_id, status="completed", **changes)
    await reaper._check_stale_teams(utc_now(), repo)
    assert (await repo.get_team(team_id)).status == "completed"


@pytest.mark.parametrize("change", ["name", "config", "project", "owner", "time"])
async def test_startup_team_snapshot_changes_refuse_without_generic_fallback(container, change):
    repo, _, _, _, agent_id, team_id = container
    busy = await repo.update_agent(agent_id, status="busy")
    team = await repo.get_team(team_id)
    changes = {
        "name": {"name": "changed-name"}, "config": {"config": {**team.config, "note": "changed"}},
        "project": {"project_id": None},
        "owner": {"config": {**team.config, "owner_session_id": str(uuid4())}},
        "time": {"summary": "changes timestamp"},
    }
    await repo.update_team(team_id, **changes[change])
    assert await repo.startup_reconcile_codex_owner(busy, team, status="waiting", now=utc_now()) is None
    assert (await repo.get_agent(agent_id)).status == "busy"


async def test_auto_close_cas_conflict_logs_no_success_or_audit(container, monkeypatch, caplog):
    import logging

    repo, _, reaper, _, agent_id, team_id = container
    original = repo.auto_close_codex_session

    async def conflict(team, members, *, now):
        await repo.update_agent(agent_id, last_active_at=now)
        return await original(team, members, now=now)

    monkeypatch.setattr(repo, "auto_close_codex_session", conflict)
    with caplog.at_level(logging.DEBUG, logger="aiteam.api.state_reaper"):
        await reaper._check_stale_teams(utc_now(), repo)
    assert "deferred Codex container" in caplog.text
    assert "auto-closed Codex container" not in caplog.text
    assert (await repo.get_team(team_id)).status == "active"
    assert not any(record.data.get("reason") == "codex_session_auto_close"
                   for record in await repo.list_events(event_type="agent.updated"))
