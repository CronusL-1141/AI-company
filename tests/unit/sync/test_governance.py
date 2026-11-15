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
    for scope in (STANDALONE, AIR_CENTER):
        await StateReaper(repo, EventBus(repo=repo), scope=scope)._reap_cycle_for_repo(repo)

    async def claim():
        return []

    await StateReaper(repo, EventBus(repo=repo), scope=AIR_EDGE, wake_claimer=claim)._reap_cycle_for_repo(repo)
    every = ["meeting_expiry", "stale_teams", "purge_containers", "agent_liveness", "watermark_backfill",
             "usage_remeasure", "scheduled_tasks", "workflow_ingest"]
    assert ran[STANDALONE] == ran[AIR_CENTER] == every
    # An edge claims the wakes of its sessions instead of scheduling them.
    assert ran[AIR_EDGE] == ["stale_teams", "purge_containers", "agent_liveness", "watermark_backfill",
                             "usage_remeasure", "wake_claims", "workflow_ingest"]


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


@pytest.fixture(scope="module")
def routes():
    from aiteam.api.app import create_app
    from aiteam.sync.routing import RouteTable

    app = create_app()
    return RouteTable(app.routes), app.routes


class _Writer:
    """Stands in for the edge's shared-face write path, as strict as the real API.

    The request must name a shared-face write route of the real application and its
    body must pass that route's own body model; a request the API would refuse gets
    the API's answer (404 or 422), not a 200. ``status`` simulates the center's
    answer to a request that passed.
    """

    def __init__(self, routes, status: int = 200) -> None:
        self.table, self.app_routes = routes
        self.calls: list[tuple[str, str, dict]] = []
        self.status = status

    async def __call__(self, method: str, path: str, payload: dict) -> tuple[int, dict]:
        from aiteam.sync.routing import _flatten

        self.calls.append((method, path, payload))
        match = self.table.match(method, path)
        if match is None or not match.shared_write:
            return 404, {"detail": "no such shared-face write"}
        route = next(r for r in _flatten(self.app_routes) if r.path == match.template and method in r.methods)
        if route.body_field is not None:
            _, errors = route.body_field.validate(payload, {}, loc=("body",))
            if errors:
                return 422, {"detail": errors}
        return self.status, {}


async def test_an_edge_resets_its_stuck_agents_tasks_through_the_center(node, routes) -> None:
    repo, path = node
    team_id, agent_id = await _team_with_agent(repo, path, "air")
    task = await repo.create_task(team_id, "stuck work", assigned_to=agent_id, status="running")
    _sql(path, "UPDATE agents SET last_active_at = ?", LONG_AGO)
    writer = _Writer(routes)
    recovered = await WatchdogChecker(repo, AIR_EDGE, shared_writer=writer).auto_recover_stuck_agents(team_id)
    assert recovered[0]["reset_tasks"] == [task.id]  # both requests passed the real routes' validation
    assert writer.calls[0] == ("PUT", f"/api/tasks/{task.id}", {"status": "pending", "assigned_to": None})
    assert writer.calls[1][:2] == ("POST", f"/api/tasks/{task.id}/memo")
    # Nothing shared was written here: the replica changes when the center's version arrives.
    assert _sql(path, "SELECT status FROM tasks WHERE id = ?", task.id) == [("running",)]
    assert _sql(path, "SELECT status FROM agents WHERE id = ?", agent_id) == [("waiting",)]
    refused = _Writer(routes, status=503)
    _sql(path, "UPDATE agents SET status = 'busy'")
    again = await WatchdogChecker(repo, AIR_EDGE, shared_writer=refused).auto_recover_stuck_agents(team_id)
    assert again[0]["reset_tasks"] == [] and len(refused.calls) == 1  # no memo for a reset that failed


async def test_an_edge_closing_a_stale_team_asks_the_center_to_conclude_its_meetings(node, routes) -> None:
    repo, path = node
    team = await repo.create_team(name="team-air", mode="coordinate")
    _origin(path, "teams", team.id, "air")
    meeting = await repo.create_meeting(team_id=team.id, topic="left open", participants=[])
    _sql(path, "UPDATE teams SET created_at = ?", LONG_AGO)
    _sql(path, "UPDATE meetings SET created_at = ?", LONG_AGO)
    writer = _Writer(routes)
    await StateReaper(repo, EventBus(repo=repo), scope=AIR_EDGE, shared_writer=writer)._check_stale_teams(
        utc_now(), repo)
    assert [call[:2] for call in writer.calls] == [("PUT", f"/api/meetings/{meeting.id}/conclude")]
    assert writer.calls[0][2]["validate_attendance"] is False
    assert _sql(path, "SELECT status FROM meetings WHERE id = ?", meeting.id) == [("active",)]


async def _due_wake(repo, path, origin: str) -> str:
    task = await repo.create_scheduled_task(name=f"wake-{origin or 'legacy'}", interval_seconds=600,
                                            action_type="wake_agent", action_config={"agent_name": "a"},
                                            team_id="t", next_run_at=utc_now() - timedelta(minutes=1))
    _origin(path, "scheduled_tasks", task.id, origin)
    return task.id


async def test_the_center_runs_its_own_wakes_and_leaves_the_others_to_be_claimed(node, monkeypatch) -> None:
    from aiteam.sync.wakes import Wakes

    repo, path = node
    wakes = {origin: await _due_wake(repo, path, origin) for origin in ("mini", "air", "", "ipad-1234")}
    ran: list[str] = []

    async def run(self, task, now, repo=None):
        ran.append(task.id)

    monkeypatch.setattr(StateReaper, "_execute_scheduled_action", run)
    center = Wakes(repo, node_id="mini")
    # Before the center's own pass: the claim must not take the center's wake, nor a third node's.
    claimed = await center.claim("air")
    assert sorted(task.id for task in claimed) == sorted([wakes["air"], wakes[""]])
    assert await center.claim("air") == []  # advanced: claimed once
    center_scope = GovernanceScope(origins=("mini",), shared=True)
    await StateReaper(repo, EventBus(repo=repo), scope=center_scope)._check_scheduled_tasks(utc_now(), repo)
    assert ran == [wakes["mini"]]
    assert await center.awaiting_claim() == 1  # the third node's, until it claims it
    assert [task.id for task in await center.claim("ipad-1234")] == [wakes["ipad-1234"]]
    assert await center.awaiting_claim() == 0


async def test_an_edge_runs_the_wakes_it_claimed(node, monkeypatch) -> None:
    repo, path = node
    wake = await repo.get_scheduled_task(await _due_wake(repo, path, "air"))
    ran: list[str] = []

    async def run(self, task, now, repo=None):
        ran.append(task.id)

    async def claim():
        return [wake]

    monkeypatch.setattr(StateReaper, "_execute_scheduled_action", run)
    reaper = StateReaper(repo, EventBus(repo=repo), scope=AIR_EDGE, wake_claimer=claim)
    await reaper._run_claimed_wakes(utc_now(), repo)
    assert ran == [wake.id]


async def test_the_governance_stub_refuses_what_the_api_refuses(routes) -> None:
    writer = _Writer(routes)
    assert (await writer("PUT", "/api/tasks/t1", {"status": 5}))[0] == 422
    assert (await writer("POST", "/api/tasks/t1/memo", {"author": "watchdog"}))[0] == 422  # content missing
    assert (await writer("POST", "/api/hooks/event", {}))[0] == 404  # not a shared-face write


async def test_the_patrol_keeps_the_edges_writer(node, routes) -> None:
    """The runner builds a checker per cycle: it must still reset through the center."""
    from aiteam.loop.watchdog import WatchdogRunner

    repo, path = node
    team_id, agent_id = await _team_with_agent(repo, path, "air")
    task = await repo.create_task(team_id, "stuck work", assigned_to=agent_id, status="running")
    _sql(path, "UPDATE agents SET last_active_at = ?", LONG_AGO)
    writer = _Writer(routes)
    runner = WatchdogRunner(checker=WatchdogChecker(repo, AIR_EDGE, shared_writer=writer),
                            event_bus=EventBus(repo=repo))
    await runner._run_cycle_for_repo(repo)
    assert ("PUT", f"/api/tasks/{task.id}", {"status": "pending", "assigned_to": None}) in writer.calls
