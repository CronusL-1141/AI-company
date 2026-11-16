"""Multi-host sync, phase 2 acceptance: the edge's session face goes up to the center.

Two real nodes from one baseline (sync_cluster.Cluster), real faults: the tunnel is
cut and slowed, API processes are SIGKILLed. Sessions run on the edge through the
hook endpoint, as the hook scripts post them. What happened is read back from both
databases through fresh connections, and every test ends with the check that the
center holds exactly the edge's session rows (one writer per origin).
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import timedelta
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parent))

from sync_cluster import CENTER_ID, EDGE_ID, Cluster, wait_for  # noqa: E402

from aiteam.clock import utc_now  # noqa: E402
from aiteam.sync import hlc  # noqa: E402
from aiteam.types import (  # noqa: E402
    PlanUsageSnapshot,
    PricingAccount,
    PricingPlanSnapshot,
    PricingQuotaSnapshot,
)

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="process groups and SIGKILL")

FAST_REAPER = "0.5"


@pytest.fixture()
def cluster(tmp_path: Path):
    nodes = Cluster(tmp_path)
    try:
        nodes.cut_over()
        yield nodes
        nodes.assert_one_writer_per_origin()
    finally:
        nodes.close()


def _stamp(moment) -> str:
    return moment.strftime("%Y-%m-%d %H:%M:%S.%f")


def _bulk_events(cluster: Cluster, count: int, tag: str) -> None:
    """A day of hook traffic in one go: events written on the edge, captured like any write."""
    con = sqlite3.connect(cluster.edge.db, timeout=30)
    try:
        con.executemany(
            "INSERT INTO events (id, type, source, data, timestamp, origin_node) VALUES (?, 'cc.tool_use', ?, ?, ?, ?)",
            [(f"{tag}-{n:06d}", f"session:{tag}", json.dumps({"tool": "Bash", "n": n}), _stamp(utc_now()), EDGE_ID)
             for n in range(count)])
        con.commit()
    finally:
        con.close()


def _ok(response: httpx.Response) -> dict:
    assert response.status_code in (200, 201), response.text
    return response.json()


# -- acceptance 1 -------------------------------------------------------------------

def test_an_edge_session_shows_on_the_center_within_seconds_tagged_with_its_node(cluster: Cluster) -> None:
    team = cluster.edge.drive_session("a1a1a1a1-air-session", subagents=("team-member", "reviewer"), tools=2)
    with cluster.center.client() as center:
        def seen() -> bool:
            teams = center.get("/api/teams", params={"origin": EDGE_ID}).json().get("data") or []
            agents = center.get(f"/api/teams/{team}/agents").json().get("data") or []
            activities = center.get(f"/api/teams/{team}/activities").json().get("data") or []
            events = center.get("/api/events", params={"origin": EDGE_ID, "limit": 200}).json().get("data") or []
            return (any(t["id"] == team for t in teams) and len(agents) >= 3 and activities
                    and any(e["type"] == "cc.tool_use" for e in events))
        wait_for(seen, timeout=5, interval=0.1, what="the edge's session on the center")
        agents = center.get(f"/api/teams/{team}/agents").json()["data"]
        activities = center.get(f"/api/teams/{team}/activities").json()["data"]
        assert {a["origin_node"] for a in agents} == {EDGE_ID}
        assert {a["origin_node"] for a in activities} == {EDGE_ID}
        assert center.get("/api/teams", params={"origin": CENTER_ID}).json()["data"] == []
        nodes = center.get("/api/sync/nodes").json()
    assert nodes["role"] == "center" and set(nodes["nodes"]) == {CENTER_ID, EDGE_ID}
    with cluster.edge.client() as edge, cluster.center.client() as center:  # the backup waits on the edge only
        assert "跨机阶段未上线" in edge.get("/api/assets/status").json()["data"]["paused"]
        assert center.get("/api/assets/status").json()["data"]["paused"] == ""
    cluster.center.stop()
    cluster.center.start()  # read again after a restart: it is in the center's database
    with cluster.center.client() as center:
        assert [t["origin_node"] for t in center.get("/api/teams", params={"origin": EDGE_ID}).json()["data"]
                if t["id"] == team] == [EDGE_ID]


# -- acceptance 2 -------------------------------------------------------------------

def test_the_center_never_reaps_an_edge_session(tmp_path: Path) -> None:
    """The center's reaper governs its own sessions only: an idle edge session older than
    the reaper's threshold stays active there, while the center's own one is closed."""
    cluster = Cluster(tmp_path)
    try:
        cluster.cut_over(center_env={"SYNC_TEST_REAPER": FAST_REAPER})
        edge_team = cluster.edge.drive_session("b2b2b2b2-air-idle", subagents=())
        center_team = cluster.center.drive_session("c3c3c3c3-mini-idle", subagents=())
        long_ago = _stamp(utc_now() - timedelta(hours=3))
        for node, team in ((cluster.edge, edge_team), (cluster.center, center_team)):
            node.write("UPDATE agents SET status = 'waiting', last_active_at = ? WHERE team_id = ?", long_ago, team)
            node.write("UPDATE teams SET created_at = ?, updated_at = ? WHERE id = ?", long_ago, long_ago, team)
        cluster.wait_uploaded()
        wait_for(lambda: cluster.center.sql("SELECT status FROM teams WHERE id = ?", center_team) == [("completed",)],
                 timeout=30, what="the center to reap its own idle session")
        wait_for(lambda: cluster.center.sql("SELECT last_active_at FROM agents WHERE team_id = ?", edge_team)
                 == [(long_ago,)], what="the edge's backdated session on the center")
        for _ in range(4):  # several more reaper cycles
            cluster.center.drive_session("d4d4d4d4-mini-tick", subagents=())
        assert cluster.center.sql("SELECT status, origin_node FROM teams WHERE id = ?", edge_team) == [
            ("active", EDGE_ID)]
        with cluster.center.client() as center:
            sessions = center.get(f"/api/projects/{cluster.project_id}/summary").json()["sessions"]
        remote = [s for s in sessions if s["session_id"] == "b2b2b2b2-air-idle"]
        assert remote and remote[0]["origin_node"] == EDGE_ID
        assert remote[0]["status_source"] == "remote_observation"
        cluster.assert_one_writer_per_origin()
    finally:
        cluster.close()


# -- acceptance 3 -------------------------------------------------------------------

def test_a_day_offline_goes_up_complete_with_the_operations_in_their_place(cluster: Cluster) -> None:
    cluster.tunnel.cut()
    cluster.wait_detached()
    team = cluster.edge.drive_session("e5e5e5e5-air-offline", tools=3)
    _bulk_events(cluster, 3000, "offline-day")
    with cluster.edge.client() as edge:
        meeting = _ok(edge.post(f"/api/teams/{team}/meetings", json={"topic": "offline", "participants": ["a"]}))
        _bulk_events(cluster, 1000, "after-meeting")
        task = _ok(edge.post(f"/api/projects/{cluster.project_id}/tasks", json={"title": "offline task"}))
    cluster.tunnel.open()
    cluster.wait_attached(timeout=60)
    cluster.wait_uploaded(timeout=60)
    assert cluster.center.sql("SELECT count(*) FROM events WHERE id LIKE 'offline-day-%' OR id LIKE 'after-meeting-%'"
                              ) == [(4000,)]
    assert cluster.center.sql("SELECT team_id FROM meetings WHERE id = ?", meeting["data"]["id"]) == [(team,)]
    assert cluster.center.sql("SELECT count(*) FROM tasks WHERE id = ?", task["data"]["id"]) == [(1,)]
    assert cluster.edge.sql("SELECT DISTINCT state FROM sync_outbox") == [("acked",)]
    assert cluster.center.sql("SELECT count(*) FROM sync_conflicts") == [(0,)]


# -- acceptance 4 -------------------------------------------------------------------

def test_usage_rows_arrive_as_observed_and_the_center_does_not_project_them(cluster: Cluster) -> None:
    team = cluster.edge.drive_session("f6f6f6f6-air-usage", subagents=("team-member",))
    key = "a" * 64
    cluster.edge.write("UPDATE agents SET input_tokens = 1234, output_tokens = 567 WHERE team_id = ?",
                       team)
    for n in range(3):
        cluster.edge.write(
            "INSERT INTO codex_usage_observations (observation_id, source_namespace, source_id, saved_at, payload, "
            "seq, kind, occurred_at, response_id, session_id, origin_node) VALUES (?, 'ns', 'src', ?, '{}', "
            "(SELECT coalesce(max(seq), 0) + 1 FROM codex_usage_observations), 'ledger', ?, ?, 's1', ?)",
            f"air-fact-{n}", _stamp(utc_now()), _stamp(utc_now()), f"r-{n}", EDGE_ID)
    quota = PricingQuotaSnapshot(snapshot_id="air-quota", account_key=key, limit_id="codex", used_percent=12,
                                 window_duration_ms=18_000_000, resets_at=utc_now() + timedelta(hours=4),
                                 observed_at=utc_now(), source="codex_app_server")
    cluster.edge.write(
        "INSERT INTO account_usage_snapshots (snapshot_id, account_key, observed_at, payload, origin_node) "
        "VALUES ('air-quota', ?, ?, ?, ?)", key, _stamp(utc_now()), quota.model_dump_json(), EDGE_ID)
    cluster.wait_uploaded()
    edge_tokens = cluster.edge.sql("SELECT id, input_tokens, output_tokens FROM agents "
                                   "WHERE team_id = ? ORDER BY id", team)
    assert cluster.center.sql("SELECT id, input_tokens, output_tokens FROM agents "
                              "WHERE team_id = ? ORDER BY id", team) == edge_tokens
    edge_facts = cluster.edge.sql("SELECT observation_id, seq FROM codex_usage_observations "
                                  "WHERE origin_node = ? ORDER BY seq", EDGE_ID)
    center_facts = cluster.center.sql("SELECT observation_id, origin_seq, seq FROM codex_usage_observations "
                                      "WHERE origin_node = ? ORDER BY origin_seq", EDGE_ID)
    assert [(o, s) for o, s, _ in center_facts] == edge_facts  # the edge's numbers, kept beside the center's
    assert cluster.center.sql("SELECT count(*) FROM account_usage_snapshots WHERE snapshot_id = 'air-quota' "
                              "AND origin_node = ?", EDGE_ID) == [(1,)]
    # The center process derives from its own rows only: its partition is itself, and
    # the edge's quota observation is shown as the edge's, not folded into the center's.
    observed = utc_now()
    plan = PlanUsageSnapshot(
        snapshot_id="air-plan", account_key=key, limit_id="codex", used_percent=12, window_duration_ms=18_000_000,
        resets_at=observed + timedelta(hours=4), observed_at=observed, activity_observed_at=observed,
        activity_tokens=1000, activity_scope="c" * 64, source="codex_account_activity")
    cluster.edge.write("INSERT INTO account_usage_accounts (account_key, created_at, payload, origin_node) "
                       "VALUES (?, ?, ?, ?)", key, _stamp(utc_now()),
                       json.dumps({"account_key": key, "label": "work", "created_at": utc_now().isoformat()}), EDGE_ID)
    cluster.edge.write("INSERT INTO account_plan_snapshots (id, account_key, observed_at, payload, origin_node) "
                       "VALUES ('air-plan', ?, ?, ?, ?)", key, _stamp(utc_now()), plan.model_dump_json(), EDGE_ID)
    cluster.wait_uploaded()
    with cluster.center.client() as center, cluster.edge.client() as edge:
        assert center.get("/api/sync/status").json()["own_origins"] == [CENTER_ID]
        assert edge.get("/api/sync/status").json()["own_origins"] == ["", EDGE_ID]
        elsewhere = center.get("/api/account-usage/other-nodes").json()["data"]["items"]
        assert [(item["origin_node"], item["snapshot_id"]) for item in elsewhere] == [(EDGE_ID, "air-plan")]
        assert edge.get("/api/account-usage/other-nodes").json()["data"]["items"] == []
        raw = center.get(f"/api/account-usage/{key}", params={"include_pricing": "false"})
        assert raw.status_code == 200, raw.text
        center_view = raw.json()["data"]
        edge_view = edge.get(f"/api/account-usage/{key}", params={"include_pricing": "false"}).json()["data"]
    assert center_view["plan_estimates"] == [] and len(edge_view["plan_estimates"]) == 1


# -- acceptance 5 -------------------------------------------------------------------

def test_killing_either_side_mid_upload_resumes_from_the_cursor_without_duplicates(cluster: Cluster) -> None:
    cluster.tunnel.cut()
    cluster.wait_detached()
    _bulk_events(cluster, 12000, "killed-upload")
    cluster.tunnel.delay = 0.02
    cluster.tunnel.open()
    def center_has() -> int:
        return cluster.center.sql("SELECT count(*) FROM events WHERE id LIKE 'killed-upload-%'")[0][0]

    wait_for(lambda: 0 < center_has() < 12000, timeout=60, what="the upload to be under way")
    cluster.edge.kill()
    taken = center_has()
    cluster.edge.start(**cluster.edge.started_with)
    wait_for(lambda: center_has() > taken, timeout=60, what="the upload to resume")
    if center_has() < 12000:
        cluster.center.kill()
        cluster.center.start(**cluster.center.started_with)
    cluster.converge(timeout=120)
    assert center_has() == 12000
    assert cluster.center.sql("SELECT count(*), count(DISTINCT id) FROM events WHERE id LIKE 'killed-upload-%'") == [
        (12000, 12000)]
    intake = cluster.center.sql("SELECT cursor FROM sync_intake WHERE node = ?", EDGE_ID)[0][0]
    assert intake == cluster.edge.sql("SELECT up_cursor FROM sync_peers")[0][0]


# -- acceptance 6 -------------------------------------------------------------------

def test_an_edge_purging_empty_session_teams_removes_them_on_the_center_only(tmp_path: Path) -> None:
    cluster = Cluster(tmp_path)
    try:
        cluster.cut_over(SYNC_TEST_REAPER=FAST_REAPER)
        shell = ("INSERT INTO teams (id, name, mode, status, summary, config, created_at, updated_at, origin_node) "
                 "VALUES (?, ?, 'coordinate', 'active', '', '{\"kind\": \"session\"}', ?, ?, ?)")
        now, long_ago = _stamp(utc_now()), _stamp(utc_now() - timedelta(days=10))
        cluster.edge.write(shell, "t-air-shell", "session-air00000", now, now, EDGE_ID)
        cluster.center.write(shell, "t-mini-shell", "session-mini0000", long_ago, long_ago, CENTER_ID)
        cluster.center.write("UPDATE teams SET status = 'completed', completed_at = ? WHERE id = 't-mini-shell'",
                             long_ago)
        wait_for(lambda: cluster.center.sql("SELECT origin_node FROM teams WHERE id = 't-air-shell'") == [(EDGE_ID,)],
                 what="the edge's shell on the center")
        cluster.edge.write("UPDATE teams SET status = 'completed', completed_at = ?, updated_at = ? "
                           "WHERE id = 't-air-shell'", long_ago, long_ago)
        wait_for(lambda: cluster.edge.sql("SELECT count(*) FROM teams WHERE id = 't-air-shell'") == [(0,)],
                 timeout=30, what="the edge's reaper to purge its shell")
        cluster.wait_uploaded()
        assert cluster.center.sql("SELECT count(*) FROM teams WHERE id = 't-air-shell'") == [(0,)]
        assert cluster.center.sql("SELECT status FROM teams WHERE id = 't-mini-shell'") == [("completed",)]
        cluster.assert_one_writer_per_origin()
    finally:
        cluster.close()


# -- acceptance 7 -------------------------------------------------------------------

def _team_writes(edge: httpx.Client, team: str, agent_name: str, tag: str) -> list[httpx.Response]:
    return [
        edge.post(f"/api/teams/{team}/meetings", json={"topic": f"meeting {tag}", "participants": [agent_name]}),
        edge.post(f"/api/teams/{team}/issues", json={"title": f"issue {tag}"}),
        edge.post(f"/api/teams/{team}/tasks/run", json={"description": f"run {tag}", "title": f"run {tag}",
                                                       "assigned_to": agent_name}),
    ]


def test_team_addressed_writes_of_an_edge_session_succeed_online_and_offline(cluster: Cluster) -> None:
    """Online, a forward carries the session face it waits for; offline, a session that
    began while offline goes up before the writes that address its team are replayed."""
    online_team = cluster.edge.drive_session("a7a7a7a7-air-online", subagents=("team-member",))
    agent_of = "SELECT name FROM agents WHERE team_id = ? AND role != 'leader' LIMIT 1"
    with cluster.edge.client() as edge:
        online = _team_writes(edge, online_team, cluster.edge.sql(agent_of, online_team)[0][0], "online")
        assert [r.status_code for r in online] == [201, 200, 200], [r.text for r in online]
        assert all(r.headers.get("x-aiteam-sync") is None for r in online)  # forwarded, not staged
        cluster.tunnel.cut()
        cluster.wait_detached()
        offline_team = cluster.edge.drive_session("b7b7b7b7-air-offline", subagents=("team-member",))
        agent = cluster.edge.sql(agent_of, offline_team)[0][0]
        offline = _team_writes(edge, offline_team, agent, "offline")
        assert [r.status_code for r in offline] == [201, 200, 200], [r.text for r in offline]
    cluster.tunnel.open()
    cluster.converge()
    for team, tag in ((online_team, "online"), (offline_team, "offline")):
        assert cluster.center.sql("SELECT topic FROM meetings WHERE team_id = ?", team) == [(f"meeting {tag}",)]
        assert cluster.center.sql("SELECT count(*) FROM tasks WHERE team_id = ?", team) == [(2,)]
    assert cluster.edge.sql("SELECT DISTINCT state FROM sync_outbox") == [("acked",)]
    assert cluster.center.sql("SELECT count(*) FROM sync_conflicts") == [(0,)]
    # task_run assigned the agent: the edge wrote its current task (handed back), the center has it as well
    current = "SELECT current_task FROM agents WHERE team_id = ? AND name = ?"
    assert cluster.center.sql(current, offline_team, agent) == cluster.edge.sql(current, offline_team, agent) == \
        [("run offline",)]


def test_worked_on_edges_of_an_edge_session_are_the_ones_a_local_write_makes(cluster: Cluster) -> None:
    """A memo of an edge session's author binds the agent it binds locally: offline, the
    staged write binds the earlier of two same-named agents; a later namesake created
    before the replay must not take the edge on the center (the session face goes up
    to the operation's position first)."""
    session = "a8a8a8a8-air-author"
    team = cluster.edge.drive_session(session, subagents=("os-builder",))
    with cluster.edge.client() as edge:
        online_task = _ok(edge.post(f"/api/projects/{cluster.project_id}/tasks", json={"title": "online"}))["data"]
        _ok(edge.post(f"/api/tasks/{online_task['id']}/memo", json={"content": "progress", "author": "os-builder"},
                      headers={"X-CC-Session-Id": session}))
        cluster.tunnel.cut()
        cluster.wait_detached()
        offline_task = _ok(edge.post(f"/api/projects/{cluster.project_id}/tasks", json={"title": "offline"}))["data"]
        _ok(edge.post(f"/api/tasks/{offline_task['id']}/memo", json={"content": "progress", "author": "os-builder"},
                      headers={"X-CC-Session-Id": session}))
    staged = cluster.edge.sql("SELECT from_id FROM knowledge_links WHERE to_id = ? AND link_type = 'worked_on'",
                              offline_task["id"])
    first = cluster.edge.sql("SELECT id FROM agents WHERE team_id = ? AND name = 'os-builder'", team)
    assert staged == first and len(first) == 1
    cluster.edge.hook(session, "SubagentStart", agent_id=f"{session}-agent-later", agent_type="os-builder")
    wait_for(lambda: len(cluster.edge.sql("SELECT id FROM agents WHERE team_id = ? AND name = 'os-builder'",
                                          team)) == 2, what="a later namesake in the session")
    cluster.tunnel.open()
    cluster.converge()
    for task in (online_task, offline_task):
        links = "SELECT from_id FROM knowledge_links WHERE to_id = ? AND link_type = 'worked_on'"
        assert cluster.center.sql(links, task["id"]) == cluster.edge.sql(links, task["id"]) == first


# -- acceptance 8 -------------------------------------------------------------------

def test_forwarded_and_replayed_writes_show_in_the_edges_own_events_and_trace(cluster: Cluster) -> None:
    def work(edge: httpx.Client, title: str) -> str:
        task = _ok(edge.post(f"/api/projects/{cluster.project_id}/tasks", json={"title": title}))["data"]["id"]
        _ok(edge.put(f"/api/tasks/{task}", json={"status": "running"}))
        _ok(edge.post(f"/api/tasks/{task}/memo", json={"content": f"memo of {title}"}))
        return task

    def changed(node, task: str) -> list[tuple]:
        return node.sql("SELECT id, origin_node FROM events WHERE type = 'task.status_changed' "
                        "AND json_extract(data, '$.task_id') = ?", task)

    with cluster.edge.client() as edge:
        online = work(edge, "seen here")
        assert [origin for _, origin in changed(cluster.edge, online)] == [EDGE_ID]  # at once, not after a sync
        cluster.tunnel.cut()
        cluster.wait_detached()
        offline = work(edge, "seen later")
        assert changed(cluster.edge, offline) == []  # staged: the center emits it
    cluster.tunnel.open()
    cluster.converge()
    with cluster.edge.client() as edge:
        listed = edge.get("/api/events", params={"type": "task.status_changed", "origin": EDGE_ID}).json()["data"]
        for task, title in ((online, "seen here"), (offline, "seen later")):
            assert [e["data"]["task_id"] for e in listed].count(task) == 1
            timeline = edge.get(f"/api/tasks/{task}/execution-trace").json()["data"]["timeline"]
            assert f"memo of {title}" in [entry["content"] for entry in timeline]
    for task in (online, offline):
        assert changed(cluster.center, task) == changed(cluster.edge, task)  # the same row on both, once


# -- the project deletion (phase 1 deviation 3) ---------------------------------------

def test_deleting_a_project_deletes_each_nodes_own_session_rows(cluster: Cluster) -> None:
    with cluster.center.client() as center:
        doomed = _ok(center.post("/api/projects", json={"name": "doomed", "root_path": "/work/doomed"}))["data"]
    wait_for(lambda: cluster.edge.sql("SELECT count(*) FROM projects WHERE id = ?", doomed["id"]) == [(1,)],
             what="the project on the edge")
    edge_team = cluster.edge.drive_session("a9a9a9a9-air-doomed", subagents=("team-member",), cwd="/work/doomed")
    center_team = cluster.center.drive_session("b9b9b9b9-mini-doomed", subagents=(), cwd="/work/doomed")
    for node, team in ((cluster.edge, edge_team), (cluster.center, center_team)):
        node.write("UPDATE teams SET project_id = ? WHERE id = ?", doomed["id"], team)
    cluster.wait_uploaded()
    with cluster.edge.client() as edge:
        assert edge.delete(f"/api/projects/{doomed['id']}").status_code == 200
    wait_for(lambda: cluster.edge.sql("SELECT count(*) FROM teams WHERE id = ?", edge_team) == [(0,)],
             what="the edge to delete its own session team of the project")
    cluster.wait_uploaded()
    for node in (cluster.center, cluster.edge):
        assert node.sql("SELECT count(*) FROM teams WHERE id IN (?, ?)", edge_team, center_team) == [(0,)]
        assert node.sql("SELECT count(*) FROM events WHERE entity_id IN (?, ?)", edge_team, center_team) == [(0,)]
    assert cluster.edge.sql("SELECT count(*) FROM agents WHERE team_id = ?", edge_team)[0][0] >= 2  # agents stay


# -- review B1 and B2 -------------------------------------------------------------------

def test_a_head_that_waits_does_not_let_later_session_rows_ahead_of_it(cluster: Cluster) -> None:
    """The center answers the head 503 for a few seconds (the edge's clock runs ahead of
    the center's by more than the allowed drift); meanwhile a namesake agent appears in
    the session. The center must not see it before the memo is replayed: the memo's
    worked_on edge binds the agent the edge bound."""
    session = "c9c9c9c9-air-waiting-head"
    team = cluster.edge.drive_session(session, subagents=("os-builder",))
    cluster.wait_uploaded()
    cluster.tunnel.cut()
    cluster.wait_detached()
    ahead = utc_now() + timedelta(minutes=5, seconds=6)
    cluster.edge.write("UPDATE sync_state SET hlc = ?", hlc.encode(hlc.Hlc(int(ahead.timestamp() * 1000), 0, EDGE_ID)))
    with cluster.edge.client() as edge:
        task = _ok(edge.post(f"/api/projects/{cluster.project_id}/tasks", json={"title": "waits"}))["data"]
        _ok(edge.post(f"/api/tasks/{task['id']}/memo", json={"content": "progress", "author": "os-builder"},
                      headers={"X-CC-Session-Id": session}))
    position = cluster.edge.sql("SELECT max(json_extract(envelope, '$.session_seq')) FROM sync_outbox")[0][0]
    first = cluster.edge.sql("SELECT id FROM agents WHERE team_id = ? AND name = 'os-builder'", team)
    cluster.edge.hook(session, "SubagentStart", agent_id=f"{session}-later", agent_type="os-builder")
    cluster.tunnel.open()
    seen_cursors = []

    def head_settled() -> bool:
        cursor = cluster.center.sql("SELECT cursor FROM sync_intake WHERE node = ?", EDGE_ID)
        seen_cursors.append(cursor[0][0] if cursor else 0)
        return cluster.pending() == 0

    wait_for(head_settled, timeout=40, interval=0.05, what="the head to go through")
    assert len(seen_cursors) > 10 and max(seen_cursors[:-1]) <= position, (position, seen_cursors[-5:])
    cluster.converge()
    links = "SELECT from_id FROM knowledge_links WHERE to_id = ? AND link_type = 'worked_on'"
    assert cluster.center.sql(links, task["id"]) == cluster.edge.sql(links, task["id"]) == first


def test_facts_recorded_on_both_nodes_all_reach_the_center(cluster: Cluster) -> None:
    """Both machines record Codex usage facts, numbered in their own databases; every one
    of the edge's arrives, whatever number it has there."""
    fact = ("INSERT INTO codex_usage_observations (observation_id, source_namespace, source_id, saved_at, payload, "
            "seq, kind, occurred_at, response_id, session_id, origin_node) VALUES (?, 'ns', 'src', ?, '{}', "
            "(SELECT coalesce(max(seq), 0) + 1 FROM codex_usage_observations), 'ledger', ?, ?, 's1', ?)")
    for n in range(4):
        for node, origin in ((cluster.center, CENTER_ID), (cluster.edge, EDGE_ID)):
            node.write(fact, f"{origin}-fact-{n}", _stamp(utc_now()), _stamp(utc_now()), f"r-{origin}-{n}", origin)
    cluster.wait_uploaded()
    arrived = cluster.center.sql("SELECT observation_id, origin_seq FROM codex_usage_observations "
                                 "WHERE origin_node = ? ORDER BY origin_seq", EDGE_ID)
    assert arrived == cluster.edge.sql("SELECT observation_id, seq FROM codex_usage_observations "
                                       "WHERE origin_node = ? ORDER BY seq", EDGE_ID)
    assert len(arrived) == 4
    assert cluster.center.sql("SELECT count(DISTINCT seq), count(*) FROM codex_usage_observations") == [(8, 8)]
    assert cluster.center.sql("SELECT sum(skipped_foreign) FROM sync_intake") == [(0,)]


# -- review B3: a node whose identity file goes missing ----------------------------------

def _without_identity(node) -> Path:
    """Restart ``node`` with its node.json moved aside; returns where it went."""
    node.stop()
    identity = node.data / "node.json"
    away = identity.with_suffix(".away")
    identity.rename(away)
    node.start(**node.started_with)
    return away


def _with_identity(node, away: Path) -> None:
    node.stop()
    away.rename(node.data / "node.json")
    node.start(**node.started_with)


def _status(node) -> dict:
    with node.client() as client:
        return client.get("/api/sync/status").json()


def test_a_center_without_its_identity_file_still_governs_and_records_only_its_own_rows(tmp_path: Path) -> None:
    """Review B3, case A: the center restarts without node.json. It keeps its node id for
    what it writes and governs only its own sessions (the edge's idle session stays
    active), stops serving its edge, and says so; once node.json is back, nothing diverges
    and no row is left without a node to govern it."""
    cluster = Cluster(tmp_path)
    try:
        cluster.cut_over(center_env={"SYNC_TEST_REAPER": FAST_REAPER})
        edge_team = cluster.edge.drive_session("d1d1d1d1-air-idle", subagents=("team-member",))
        long_ago = _stamp(utc_now() - timedelta(hours=3))
        cluster.edge.write("UPDATE agents SET status = 'waiting', last_active_at = ? WHERE team_id = ?", long_ago,
                           edge_team)
        cluster.edge.write("UPDATE teams SET created_at = ?, updated_at = ? WHERE id = ?", long_ago, long_ago,
                           edge_team)
        cluster.wait_uploaded()
        held_from = _stamp(utc_now())
        away = _without_identity(cluster.center)
        status = _status(cluster.center)
        assert not status["healthy"] and any("同步通道已停" in problem for problem in status["problems"])
        cluster.wait_detached()
        own_team = cluster.center.drive_session("d2d2d2d2-mini-held", subagents=())
        cluster.center.write("UPDATE agents SET status = 'waiting', last_active_at = ? WHERE team_id = ?", long_ago,
                             own_team)
        cluster.center.write("UPDATE teams SET created_at = ?, updated_at = ? WHERE id = ?", long_ago, long_ago,
                             own_team)
        wait_for(lambda: cluster.center.sql("SELECT status FROM teams WHERE id = ?", own_team) == [("completed",)],
                 timeout=30, what="the held center to govern its own idle session")
        assert cluster.center.sql("SELECT origin_node FROM teams WHERE id = ?", own_team) == [(CENTER_ID,)]
        assert cluster.center.sql("SELECT status FROM teams WHERE id = ?", edge_team) == [("active",)]
        assert cluster.center.sql("SELECT DISTINCT status FROM agents WHERE team_id = ?", edge_team) == [("waiting",)]
        _with_identity(cluster.center, away)
        assert _status(cluster.center)["healthy"]
        cluster.assert_one_writer_per_origin()
        written_while_held = cluster.center.sql(
            "SELECT count(*) FROM events WHERE origin_node = '' AND timestamp > ?", held_from)
        assert written_while_held == [(0,)]  # nothing the center wrote looks like the edge's baseline
    finally:
        cluster.close()


def test_an_edge_without_its_identity_file_keeps_recording_and_queues_its_shared_writes(cluster: Cluster) -> None:
    """Review B3, case B: the edge restarts without node.json. Its sessions are still
    captured under its node id, its shared writes are staged for later, its status is red;
    once node.json is back everything reaches the center and nothing is an orphan."""
    away = _without_identity(cluster.edge)
    status = _status(cluster.edge)
    assert not status["healthy"] and status["link"] == "detached"
    team = cluster.edge.drive_session("e1e1e1e1-air-held", subagents=("team-member",))
    with cluster.edge.client() as edge:
        response = edge.post(f"/api/projects/{cluster.project_id}/tasks", json={"title": "while held"})
    assert response.status_code == 201 and response.headers.get("x-aiteam-sync") == "staged"
    task = response.json()["data"]["id"]
    assert cluster.edge.sql("SELECT origin_node FROM teams WHERE id = ?", team) == [(EDGE_ID,)]
    assert cluster.pending() == 1
    assert _status(cluster.edge)["orphan_writes"] == 0
    _with_identity(cluster.edge, away)
    cluster.converge()
    assert cluster.center.sql("SELECT title FROM tasks WHERE id = ?", task) == [("while held",)]
    assert cluster.center.sql("SELECT origin_node FROM teams WHERE id = ?", team) == [(EDGE_ID,)]
    assert _status(cluster.edge)["healthy"]


# -- review S4: every offline-capable shared write of an edge session --------------------

def _every_shared_write(edge: httpx.Client, cluster: Cluster, team: str, agent: str, tag: str) -> list[tuple]:
    """Each offline-capable shared-face write route once, from an edge session (aiteam.sync.surface).

    ``PUT /api/scheduler/wake-pause-all`` and ``wake-resume-all`` are left out: they are
    answered by ``/{task_id}`` (404) before they are reached, a routing fault outside
    the sync (noted in the review of this stage).
    """
    pid = cluster.project_id
    done: list[tuple] = []

    def call(method: str, path: str, body: dict | None = None) -> dict:
        response = edge.request(method, path, json=body if body is not None else {})
        done.append((method, path.replace(tag, "{tag}"), response.status_code))
        assert response.status_code < 300, (method, path, response.text[:300])
        payload = response.json()
        return payload.get("data") if isinstance(payload.get("data"), dict) else payload

    call("POST", "/api/context/resolve", {"cwd": f"/work/everything-{tag}", "auto_create": True})
    project = call("POST", "/api/projects", {"name": f"p-{tag}", "root_path": f"/work/p-{tag}"})
    call("PUT", f"/api/projects/{project['id']}", {"description": "edited"})
    phase = call("POST", f"/api/projects/{pid}/phases", {"name": f"phase {tag}"})
    call("PUT", f"/api/projects/{pid}/phases/{phase['id']}/status", {"status": "active"})
    task = call("POST", f"/api/projects/{pid}/tasks", {"title": f"t {tag}"})
    call("PUT", f"/api/tasks/{task['id']}", {"status": "running"})
    call("POST", f"/api/tasks/{task['id']}/memo", {"content": f"memo {tag}", "author": agent})
    call("POST", f"/api/projects/{pid}/tasks/batch",
         {"idempotency_key": f"batch-{tag}", "tasks": [{"key": "a", "title": f"batch {tag}"}]})
    call("POST", f"/api/teams/{team}/tasks/run", {"description": f"run {tag}", "title": f"run {tag}",
                                                  "assigned_to": agent})
    call("POST", f"/api/teams/{team}/tasks/decompose", {"title": f"split {tag}", "template": "bugfix"})
    issue = call("POST", f"/api/teams/{team}/issues", {"title": f"issue {tag}"})
    call("PUT", f"/api/issues/{issue['data']['id'] if 'data' in issue else issue['id']}/status",
         {"status": "investigating"})
    call("POST", f"/api/tasks/{task['id']}/diagnose")
    call("POST", f"/api/teams/{team}/failure-analysis", {"task_id": task["id"]})
    call("POST", f"/api/watchdog/verify/{task['id']}")
    call("PUT", f"/api/tasks/{task['id']}/complete", {"result": "done"})
    meeting = call("POST", f"/api/teams/{team}/meetings", {"topic": f"m {tag}", "participants": [agent]})
    call("POST", f"/api/meetings/{meeting['id']}/messages",
         {"agent_id": "a", "agent_name": agent, "content": "point", "round_number": 1})
    call("PUT", f"/api/meetings/{meeting['id']}", {"topic": f"renamed {tag}"})
    call("PUT", f"/api/meetings/{meeting['id']}/conclude", {"force": True})
    scope = {"X-Project-Id": pid}
    memory = edge.post("/api/memories", json={"content": f"direction {tag}", "kind": "directive", "scope": "project"},
                       headers=scope)
    done.append(("POST", "/api/memories", memory.status_code))
    other = edge.post("/api/memories", json={"content": f"second {tag}", "kind": "directive", "scope": "project"},
                      headers=scope)
    edge.post(f"/api/memories/{memory.json()['data']['id']}/invalidate", json={}, headers=scope)
    done.append(("POST", "/api/memories/{id}/invalidate", 200))
    invalidated = edge.post("/api/memories/invalidate", json={"content_match": f"second {tag}"}, headers=scope)
    done.append(("POST", "/api/memories/invalidate", invalidated.status_code))
    assert other.status_code == 200 and invalidated.status_code == 200, invalidated.text
    scheduled = call("POST", "/api/scheduler", {"name": f"tick {tag}", "interval_seconds": 3600,
                                                "action_type": "wake_agent", "team_id": team})
    call("PUT", f"/api/scheduler/{scheduled['id']}", {"enabled": False})
    report = edge.post("/api/reports", json={"author": tag, "topic": f"r-{tag}", "content": "body"}, headers=scope)
    done.append(("POST", "/api/reports", report.status_code))
    first = call("POST", "/api/leader-briefings", {"title": f"decide {tag}", "project_id": pid})
    second = call("POST", "/api/leader-briefings", {"title": f"skip {tag}", "project_id": pid})
    call("PUT", f"/api/leader-briefings/{first['id']}/resolve", {"resolution": "done"})
    call("PUT", f"/api/leader-briefings/{second['id']}/dismiss", {})
    call("POST", f"/api/channels/team:{tag}/messages", {"sender": "leader", "content": "hi"})
    call("POST", f"/api/channels/team:{tag}/read-cursor",
         {"reader": "leader-cc", "project_id": pid, "last_read_at": utc_now().isoformat()})
    return done


def test_every_offline_capable_shared_write_of_an_edge_session_leaves_one_writer(cluster: Cluster) -> None:
    """Every such route, from an edge session, forwarded online and staged offline then
    replayed: none is refused by the center's one-writer guard, the queue drains with no
    conflict, and the teardown comparison finds the center holding exactly the edge's
    session rows (whatever session rows these routes write go back to the edge)."""
    session = "f0f0f0f0-air-everything"
    team = cluster.edge.drive_session(session, subagents=("team-member",))
    agent = cluster.edge.sql("SELECT name FROM agents WHERE team_id = ? AND role != 'leader' LIMIT 1", team)[0][0]
    with cluster.edge.client() as edge:
        edge.headers["X-CC-Session-Id"] = session
        online = _every_shared_write(edge, cluster, team, agent, "online")
        cluster.tunnel.cut()
        cluster.wait_detached()
        offline = _every_shared_write(edge, cluster, team, agent, "offline")
    assert len({(m, p) for m, p, _ in online}) == 32 == len({(m, p) for m, p, _ in offline})
    cluster.tunnel.open()
    cluster.converge()
    assert cluster.edge.sql("SELECT DISTINCT state FROM sync_outbox") == [("acked",)]
    assert cluster.center.sql("SELECT count(*) FROM sync_conflicts WHERE status = 'pending'") == [(0,)]


# -- review S3: an anchor is set at the sample of the node that resets ------------------

def _paired_sample(node, key: str, snapshot_id: str, percent: int) -> None:
    """A price sample and its quota twin saved on ``node``, as its capture saves them."""
    observed = utc_now() - timedelta(seconds=5)
    fields = {"snapshot_id": snapshot_id, "account_key": key, "limit_id": "codex", "window_duration_ms": 604_800_000,
              "resets_at": observed + timedelta(days=3), "observed_at": observed, "used_percent": percent}
    quota = PricingQuotaSnapshot(**fields, source="codex_app_server")
    price = PricingPlanSnapshot(**fields)
    node.write("INSERT INTO account_usage_snapshots (snapshot_id, account_key, observed_at, payload, origin_node) "
               "VALUES (?, ?, ?, ?, ?)", snapshot_id, key, _stamp(observed), quota.model_dump_json(), node.node_id)
    node.write("INSERT INTO account_plan_price_snapshots (id, account_key, observed_at, payload, origin_node) "
               "VALUES (?, ?, ?, ?, ?)", snapshot_id, key, _stamp(observed), price.model_dump_json(), node.node_id)


def test_a_plan_anchor_reset_on_the_edge_is_set_at_the_edges_own_sample(cluster: Cluster) -> None:
    """The price summaries are partitioned: the edge folds its own samples. Its reset picks
    its own latest sample and the center stores it for everyone, so the edge's manual
    cycle starts there; a center that picked from its own samples would have none."""
    key = "d" * 64
    account = PricingAccount(account_key=key, label="work", created_at=utc_now())
    cluster.edge.write("INSERT INTO account_usage_accounts (account_key, created_at, payload, origin_node) "
                       "VALUES (?, ?, ?, ?)", key, _stamp(utc_now()), account.model_dump_json(), EDGE_ID)
    _paired_sample(cluster.edge, key, "air-sample-1", 20)
    with cluster.edge.client() as edge:
        response = edge.post(f"/api/account-usage/{key}/plan-anchor/reset",
                             json={"limit_id": "codex", "window_duration_ms": 604_800_000})
        assert response.status_code == 200, response.text
        anchor = response.json()["data"]
        assert anchor["snapshot_id"] == "air-sample-1" and anchor["revision"] == 1
        cluster.converge()
        assert cluster.center.sql("SELECT json_extract(payload, '$.snapshot_id'), origin_node "
                                  "FROM account_plan_price_anchors") == [("air-sample-1", EDGE_ID)]
        estimates = edge.get(f"/api/account-usage/{key}", params={"include_pricing": "false"}).json()["data"]
        assert [(e["start_snapshot_id"], e["status"]) for e in estimates["pricing_plan_estimates"]] == [
            ("air-sample-1", "collecting")]
        _paired_sample(cluster.edge, key, "air-sample-2", 21)
        again = edge.post(f"/api/account-usage/{key}/plan-anchor/reset",
                          json={"limit_id": "codex", "window_duration_ms": 604_800_000}).json()["data"]
        assert (again["snapshot_id"], again["revision"]) == ("air-sample-2", 2)


# -- review S7: a full resend leaves the center holding exactly the edge's rows ---------

def _resend(cluster: Cluster, *flags: str) -> tuple[int, str]:
    """``python3 -m aiteam.sync resend-session`` on the (stopped) edge, its hardware pinned."""
    import subprocess

    script = ("import os, sys; from aiteam.sync import node; "
              "node.hardware_uuid = lambda: os.environ['SYNC_TEST_HARDWARE']; "
              "from aiteam.sync.cli import main; sys.exit(main(sys.argv[1:]))")
    done = subprocess.run([sys.executable, "-c", script, "resend-session", "--db", str(cluster.edge.db), *flags],
                          env=cluster.edge.env(), capture_output=True, text=True, timeout=120)
    return done.returncode, done.stdout + done.stderr


def test_a_full_resend_restores_what_the_center_lost_and_removes_what_the_edge_deleted(cluster: Cluster) -> None:
    """A center restored from an old backup lacks rows the edge has and holds rows the
    edge deleted since (their deletions pruned on the edge). The resend sends the one
    and deletes the other, and only rows the edge owns: the center's own rows and
    another edge's stay."""
    cluster.edge.drive_session("a7a7a7a7-air-resend", subagents=())
    cluster.wait_uploaded()
    assert cluster.center.sql("SELECT node FROM sync_intake WHERE baseline_owner") == [(EDGE_ID,)]
    lost = cluster.edge.sql("SELECT id FROM events WHERE origin_node = ? ORDER BY timestamp LIMIT 1", EDGE_ID)[0][0]
    cluster.edge.stop()
    cluster.center.write("DELETE FROM events WHERE id = ?", lost)
    for event_id, origin in (("ghost-edge", EDGE_ID), ("ghost-baseline", ""), ("center-own", CENTER_ID),
                             ("other-edge", "mini-other")):
        cluster.center.write("INSERT INTO events (id, type, source, data, timestamp, origin_node) VALUES "
                             "(?, 'cc.tool_use', 'test', '{}', ?, ?)", event_id, _stamp(utc_now()), origin)

    code, output = _resend(cluster)
    assert code == 0, output
    assert '"deleted_on_center": {\n    "events": 2\n  }' in output
    assert cluster.center.sql("SELECT count(*) FROM events WHERE id IN ('ghost-edge', 'ghost-baseline', ?)",
                              lost) == [(2,)]  # the preview changed nothing

    code, output = _resend(cluster, "--apply")
    assert code == 0, output
    remaining = cluster.center.sql("SELECT id FROM events WHERE id IN "
                                   "('ghost-edge', 'ghost-baseline', 'center-own', 'other-edge', ?) ORDER BY id", lost)
    assert remaining == sorted([("center-own",), ("other-edge",), (lost,)])
    cluster.edge.start(**cluster.edge.started_with)
    cluster.converge()
