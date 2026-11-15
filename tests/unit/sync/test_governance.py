"""Background governance narrowed by origin: a syncing node governs only its own session rows.

Standalone (the default scope) must govern exactly what it always did; an edge or a
center skips session rows from another node, and only a center (or standalone) runs
the shared-face steps. Every outcome is read back from the database file.
"""

from __future__ import annotations

import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest

from aiteam.api.deps import _startup_reconciliation
from aiteam.api.event_bus import EventBus
from aiteam.api.state_reaper import StateReaper
from aiteam.clock import utc_now
from aiteam.loop.watchdog import WatchdogChecker
from aiteam.storage.connection import close_db
from aiteam.storage.repository import StorageRepository
from aiteam.types import GovernanceScope, WorkflowRun

STANDALONE = GovernanceScope()
AIR_EDGE = GovernanceScope(origins=("", "air"), shared=False)
AIR_CENTER = GovernanceScope(origins=("", "air"), shared=True)
LONG_AGO = (utc_now() - timedelta(days=60)).strftime("%Y-%m-%d %H:%M:%S.%f")


@pytest.fixture()
async def node(tmp_path: Path):
    path = tmp_path / "node.db"
    repo = StorageRepository(db_url=f"sqlite+aiosqlite:///{path}")
    await repo.init_db()
    yield repo, path
    await close_db()


def _sql(path: Path, statement: str, *params) -> list[tuple]:
    con = sqlite3.connect(path)
    try:
        rows = con.execute(statement, params).fetchall()
        con.commit()
        return rows
    finally:
        con.close()


def _origin(path: Path, table: str, row_id: str, origin: str) -> None:
    _sql(path, f"UPDATE {table} SET origin_node = ? WHERE id = ?", origin, row_id)


async def _team_with_agent(repo, path, origin: str, *, status: str = "busy") -> tuple[str, str]:
    team = await repo.create_team(name=f"team-{origin or 'legacy'}", mode="coordinate")
    agent = await repo.create_agent(team_id=team.id, name=f"agent-{origin or 'legacy'}", role="member")
    await repo.update_agent(agent.id, status=status)
    _origin(path, "teams", team.id, origin)
    _origin(path, "agents", agent.id, origin)
    return team.id, agent.id


async def test_governance_queries_keep_only_the_listed_origins(node) -> None:
    repo, path = node
    rows = {origin: await _team_with_agent(repo, path, origin) for origin in ("", "air", "mini")}
    for origin, (team_id, _) in rows.items():
        run = (await repo.upsert_workflow_run(WorkflowRun(wf_id=f"wf-{origin}", status="running"))).run
        _origin(path, "workflow_runs", run.id, origin)
        wake = await repo.create_wake_session("sched", f"wake-{origin}", team_id)
        _origin(path, "wake_sessions", wake.id, origin)
    _sql(path, "UPDATE wake_sessions SET started_at = ?", LONG_AGO)

    def names(items) -> set[str]:
        return {item.name for item in items}

    assert len(await repo.list_teams()) == len(await repo.list_teams(origins=None)) == 3
    assert names(await repo.list_teams(origins=AIR_EDGE.origins)) == {"team-legacy", "team-air"}
    assert [a.name for a in await repo.list_agents(rows["mini"][0], origins=AIR_EDGE.origins)] == []
    busy = await repo.list_busy_agents_by_team(origins=AIR_EDGE.origins)
    assert set(busy) == {rows[""][0], rows["air"][0]}
    runs = await repo.list_workflow_runs(status="running", origins=AIR_EDGE.origins)
    assert {r.wf_id for r in runs} == {"wf-", "wf-air"}
    assert await repo.cleanup_old_sessions(days=30, origins=AIR_EDGE.origins) == 2
    assert _sql(path, "SELECT agent_name FROM wake_sessions") == [("wake-mini",)]


async def test_a_reaper_skips_shared_steps_unless_it_governs_the_shared_face(node, monkeypatch) -> None:
    repo, _ = node
    ran: dict[str, list[str]] = {}

    async def record(self, steps, *, deadline):
        ran[self._scope] = [name for name, _ in steps]

    monkeypatch.setattr(StateReaper, "_run_cycle_steps", record)
    for scope in (STANDALONE, AIR_CENTER, AIR_EDGE):
        await StateReaper(repo, EventBus(repo=repo), scope=scope)._reap_cycle_for_repo(repo)
    every = ["meeting_expiry", "stale_teams", "purge_containers", "agent_liveness", "watermark_backfill",
             "usage_remeasure", "scheduled_tasks", "workflow_ingest"]
    assert ran[STANDALONE] == ran[AIR_CENTER] == every
    assert ran[AIR_EDGE] == [name for name in every if name not in ("meeting_expiry", "scheduled_tasks")]


@pytest.mark.parametrize(("scope", "closed"), [
    (STANDALONE, {"team-legacy", "team-air", "team-mini"}),
    (AIR_EDGE, {"team-legacy", "team-air"}),
], ids=["standalone", "edge"])
async def test_a_stale_team_is_closed_only_by_its_own_node(node, scope, closed) -> None:
    repo, path = node
    for origin in ("", "air", "mini"):
        team = await repo.create_team(name=f"team-{origin or 'legacy'}", mode="coordinate")
        _origin(path, "teams", team.id, origin)
    _sql(path, "UPDATE teams SET created_at = ?", LONG_AGO)
    await StateReaper(repo, EventBus(repo=repo), scope=scope)._check_stale_teams(utc_now(), repo)
    assert {name for (name,) in _sql(path, "SELECT name FROM teams WHERE status = 'completed'")} == closed


@pytest.mark.parametrize(("scope", "task_status"), [(STANDALONE, "pending"), (AIR_EDGE, "running")],
                         ids=["standalone", "edge"])
async def test_the_watchdog_recovers_its_own_agents_and_leaves_tasks_to_the_center(
    node, scope, task_status,
) -> None:
    repo, path = node
    rows = {origin: await _team_with_agent(repo, path, origin) for origin in ("air", "mini")}
    team_id, agent_id = rows["air"]
    task = await repo.create_task(team_id, "stuck work", assigned_to=agent_id, status="running")
    _sql(path, "UPDATE agents SET last_active_at = ?", LONG_AGO)

    checker = WatchdogChecker(repo, scope)
    for team, _ in rows.values():
        await checker.auto_recover_stuck_agents(team)
    statuses = dict(_sql(path, "SELECT name, status FROM agents"))
    assert statuses["agent-air"] == "waiting"
    assert statuses["agent-mini"] == ("waiting" if scope is STANDALONE else "busy")
    assert _sql(path, "SELECT status FROM tasks WHERE id = ?", task.id) == [(task_status,)]
    assert (await checker.recover_failed_tasks(team_id)) == []


async def test_startup_reconciliation_resets_only_its_own_agents(node) -> None:
    repo, path = node
    for origin in ("", "air", "mini"):
        await _team_with_agent(repo, path, origin)
    await _startup_reconciliation(repo, AIR_EDGE)
    assert dict(_sql(path, "SELECT name, status FROM agents")) == {
        "agent-legacy": "waiting", "agent-air": "waiting", "agent-mini": "busy"}


@pytest.mark.parametrize(("scope", "retried"), [(STANDALONE, True), (AIR_CENTER, True), (AIR_EDGE, False)],
                         ids=["standalone", "center", "edge"])
async def test_failed_tasks_are_retried_by_the_center_only(node, scope, retried) -> None:
    repo, path = node
    team = await repo.create_team(name="team-air", mode="coordinate")
    task = await repo.create_task(team.id, "broke once", status="failed")
    results = await WatchdogChecker(repo, scope).recover_failed_tasks(team.id)
    assert [r["action"] for r in results] == (["retried"] if retried else [])
    assert _sql(path, "SELECT status FROM tasks WHERE id = ?", task.id) == [("pending" if retried else "failed",)]


@pytest.mark.parametrize(("scope", "checked"), [(STANDALONE, True), (AIR_EDGE, False)], ids=["standalone", "edge"])
async def test_task_health_is_checked_by_the_center_only(node, scope, checked) -> None:
    repo, path = node
    team = await repo.create_team(name="team-air", mode="coordinate")
    task = await repo.create_task(team.id, "waiting forever")
    _sql(path, "UPDATE tasks SET created_at = ?", LONG_AGO)
    alerts = await WatchdogChecker(repo, scope).run_all_checks(team.id)
    assert any(alert.get("task_id") == task.id for alert in alerts) is checked


@pytest.mark.parametrize(("scope", "meeting_status"), [(STANDALONE, "concluded"), (AIR_EDGE, "active")],
                         ids=["standalone", "edge"])
async def test_closing_a_stale_team_concludes_its_meetings_on_the_center_only(node, scope, meeting_status) -> None:
    repo, path = node
    team = await repo.create_team(name="team-air", mode="coordinate")
    _origin(path, "teams", team.id, "air")
    meeting = await repo.create_meeting(team_id=team.id, topic="left open", participants=[])
    _sql(path, "UPDATE teams SET created_at = ?", LONG_AGO)
    _sql(path, "UPDATE meetings SET created_at = ?", LONG_AGO)
    await StateReaper(repo, EventBus(repo=repo), scope=scope)._check_stale_teams(utc_now(), repo)
    assert _sql(path, "SELECT status FROM teams") == [("completed",)]  # the team is session-face: closed here
    assert _sql(path, "SELECT status FROM meetings WHERE id = ?", meeting.id) == [(meeting_status,)]
