"""Multi-host sync, phase 1 acceptance: two real nodes, real faults.

Every test ends with the phase 2 check that the center holds exactly the edge's
session rows (sync_cluster.Cluster.assert_one_writer_per_origin).

Every test cuts a center and an edge over from one baseline (sync_cluster.Cluster):
two API processes over two data directories, the edge reaching the center only
through a loopback tunnel. The faults are real: the tunnel is cut (its port closes,
as a dropped SSH forward's does), API processes are SIGKILLed, the center's answers
are held back past the edge's timeout. What happened is read back from the
databases through fresh connections, after restarts where the case asks for it.
"""

from __future__ import annotations

import json
import shutil
import statistics
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parent))

from sync_cluster import EDGE_ID, Cluster, wait_for  # noqa: E402

from aiteam.sync.cli import shared_face_differences  # noqa: E402

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="process groups and SIGKILL")


@pytest.fixture()
def cluster(tmp_path: Path):
    nodes = Cluster(tmp_path)
    try:
        nodes.cut_over()
        yield nodes
        nodes.assert_one_writer_per_origin()
    finally:
        nodes.close()


def _scope(cluster: Cluster) -> dict[str, str]:
    return {"X-Project-Id": cluster.project_id}


def _create_task(client: httpx.Client, cluster: Cluster, title: str) -> httpx.Response:
    response = client.post(f"/api/projects/{cluster.project_id}/tasks", json={"title": title})
    assert response.status_code == 201, response.text
    return response


def _settled(cluster: Cluster) -> None:
    """Queue empty, staged rows settled, replica caught up with the center."""
    wait_for(lambda: cluster.pending() == 0, timeout=30, what="the queue to drain")
    wait_for(lambda: cluster.edge.sql("SELECT count(*) FROM sync_tentative")[0][0] == 0, timeout=30,
             what="staged rows to settle")
    wait_for(lambda: cluster.edge.sql("SELECT cursor FROM sync_peers")[0][0]
             >= cluster.center.sql("SELECT coalesce(max(seq), 0) FROM sync_changes")[0][0],
             timeout=30, what="the replica to catch up")


def _write_four(client: httpx.Client, cluster: Cluster, tag: str) -> dict[str, str]:
    task = _create_task(client, cluster, f"task {tag}").json()["data"]["id"]
    memo = client.post(f"/api/tasks/{task}/memo", json={"content": f"memo {tag}"})
    report = client.post("/api/reports", json={"author": tag, "topic": f"report-{tag}", "content": "body"},
                         headers=_scope(cluster))
    memory = client.post("/api/memories", json={"content": f"direction {tag}", "kind": "directive",
                                                "scope": "project"}, headers=_scope(cluster))
    assert memo.status_code == 200 and report.status_code == 201 and memory.status_code == 200, \
        (memo.text, report.text, memory.text)
    return {"task": task, "memo": f"memo {tag}", "report": report.json()["id"],
            "memory": memory.json()["data"]["id"]}


def _reads_four(client: httpx.Client, cluster: Cluster, written: dict[str, str]) -> bool:
    if client.get(f"/api/tasks/{written['task']}").status_code != 200:
        return False
    memos = client.get(f"/api/tasks/{written['task']}/memo").json().get("data") or []
    memories = client.get("/api/memories", headers=_scope(cluster)).json().get("data") or []
    return (any(m["content"] == written["memo"] for m in memos)
            and client.get(f"/api/reports/{written['report']}").status_code == 200
            and any(m["id"] == written["memory"] for m in memories))


def _listening(pgid: int) -> list[str]:
    lsof = shutil.which("lsof")
    if lsof is None:
        pytest.skip("lsof is not available")
    result = subprocess.run([lsof, "-nP", "-g", str(pgid), "-a", "-iTCP", "-sTCP:LISTEN", "-Fn"],
                            capture_output=True, text=True, timeout=30, check=False)
    return [line[1:] for line in result.stdout.splitlines() if line.startswith("n")]


def test_writes_on_either_node_are_read_on_the_other_within_two_seconds(cluster: Cluster) -> None:
    """Acceptance 1 (both directions), 8 (no orphan write while online) and 10 (loopback only)."""
    with cluster.edge.client() as edge, cluster.center.client() as center:
        for writer, reader, tag in ((edge, center, "from-edge"), (center, edge, "from-center")):
            written = _write_four(writer, cluster, tag)
            started = time.monotonic()
            wait_for(lambda r=reader, w=written: _reads_four(r, cluster, w), timeout=2.0,
                     what=f"the writes {tag} on the other node")
            assert time.monotonic() - started < 2.0
        status = edge.get("/api/sync/status").json()
    assert status["link"] == "attached" and status["orphan_writes"] == 0 and status["healthy"], status
    # Rows created for the edge carry its origin on both nodes.
    assert cluster.center.sql("SELECT origin_node FROM reports WHERE topic = 'report-from-edge'") == [(EDGE_ID,)]
    for node in (cluster.center, cluster.edge):
        addresses = _listening(node.process.pid)
        assert addresses and all(address.startswith("127.0.0.1:") for address in addresses), addresses


def test_offline_work_queues_in_order_and_converges_row_by_row(cluster: Cluster) -> None:
    """Acceptance 2 (offline: writes and reads succeed, hooks as fast) and 3 (order, convergence)."""
    def hook_latencies(client: httpx.Client, session: str) -> list[float]:
        latencies = []
        for n in range(20):
            event = {"hook_event_name": "PreToolUse", "session_id": session, "cwd": "/work/shared",
                     "tool_name": "Bash", "tool_input": {"command": f"echo {n}"}, "tool_use_id": f"{session}-{n}"}
            started = time.perf_counter()
            assert client.post("/api/hooks/event", json=event).status_code == 200
            latencies.append(time.perf_counter() - started)
        return latencies

    with cluster.edge.client() as edge:
        online = hook_latencies(edge, "online-session")
        cluster.tunnel.cut()
        made = [_create_task(edge, cluster, f"offline {n}") for n in range(3)]
        assert all(r.headers.get("x-aiteam-sync") == "staged" for r in made)
        first = made[0].json()["data"]["id"]
        assert edge.put(f"/api/tasks/{first}", json={"status": "running"}).status_code == 200
        assert edge.post(f"/api/tasks/{first}/memo", json={"content": "offline memo"}).status_code == 200
        assert edge.get(f"/api/projects/{cluster.project_id}/task-wall").status_code == 200
        assert edge.get("/api/memories", headers=_scope(cluster)).status_code == 200
        assert edge.get(f"/api/tasks/{first}").json()["data"]["status"] == "running"
        offline = hook_latencies(edge, "offline-session")
        assert cluster.link() == "detached"
        queued = [row[0] for row in cluster.edge.sql(
            "SELECT op_id FROM sync_outbox WHERE state = 'pending' ORDER BY hlc")]
        assert len(queued) == 5
    # Hooks never wait for the center: the same speed offline as online.
    assert statistics.median(offline) < statistics.median(online) + 0.05, (online, offline)

    cluster.tunnel.open()
    cluster.wait_attached(timeout=30)
    _settled(cluster)
    applied = [row[0] for row in cluster.center.sql("SELECT op_id FROM sync_op_receipts ORDER BY rowid")]
    assert applied[-5:] == queued  # replayed in the order they were made
    assert cluster.center.sql("SELECT status FROM tasks WHERE id = ?", first) == [("running",)]
    assert shared_face_differences(cluster.edge.db, cluster.center.db) == []


def test_concurrent_edits_merge_by_rule_and_the_losers_can_win_after_all(cluster: Cluster) -> None:
    """Acceptance 4: status, assignee, title edited on both sides while apart."""
    with cluster.edge.client() as edge, cluster.center.client() as center:
        task = _create_task(edge, cluster, "contested").json()["data"]["id"]
        _settled(cluster)
        cluster.tunnel.cut()
        assert center.put(f"/api/tasks/{task}", json={"priority": "low"}).status_code == 200
        time.sleep(0.01)  # the edge's write below is later than this one by its clock
        staged = edge.put(f"/api/tasks/{task}", json={
            "status": "running", "assigned_to": "air-agent", "title": "edge title", "priority": "high"})
        assert staged.headers.get("x-aiteam-sync") == "staged"
        time.sleep(0.01)
        assert center.put(f"/api/tasks/{task}", json={"status": "completed", "assigned_to": "mini-agent"}) \
            .status_code == 200
        assert center.put(f"/api/tasks/{task}", json={"title": "center title"}).status_code == 200
        cluster.tunnel.open()
        cluster.wait_attached(timeout=30)
        _settled(cluster)

        row = ("SELECT status, assigned_to, title, priority FROM tasks WHERE id = ?", task)
        # completed outranks running; the later writer wins each other field.
        assert cluster.center.sql(*row) == [("completed", "mini-agent", "center title", "high")]
        assert cluster.edge.sql(*row) == cluster.center.sql(*row)
        conflicts = center.get("/api/sync/conflicts").json()["items"]
        lost = {(c["field"], c["loser"], c["rule"]) for c in conflicts if c["loser_node"] == EDGE_ID}
        assert lost == {("status", "running", "status_rank"), ("assigned_to", "air-agent", "last_writer"),
                        ("title", "edge title", "last_writer")}
        assert {(c["field"], c["loser"]) for c in conflicts if c["loser_node"] != EDGE_ID} == {("priority", "low")}
        # One briefing for the drain, counting every decision its operations met.
        briefings = cluster.center.sql("SELECT title FROM leader_briefings WHERE title LIKE ?", f"{EDGE_ID}%")
        assert [title for (title,) in briefings] == [f"{EDGE_ID} 重连后有 4 处同步冲突（0 处待处理）"]

        flipped = center.post("/api/sync/conflicts/prefer", json={"node_id": EDGE_ID}).json()["items"]
        assert {c["status"] for c in flipped} == {"flipped"} and len(flipped) == 3
        assert cluster.center.sql(*row) == [("running", "air-agent", "edge title", "high")]
        wait_for(lambda: cluster.edge.sql(*row) == cluster.center.sql(*row), timeout=5,
                 what="the flipped values on the edge")


def test_replaying_the_same_operations_three_times_changes_nothing(cluster: Cluster) -> None:
    """Acceptance 5, read back again after the center restarted."""
    with cluster.edge.client() as edge:
        cluster.tunnel.cut()
        task = _create_task(edge, cluster, "replayed").json()["data"]["id"]
        edge.post(f"/api/tasks/{task}/memo", json={"content": "on a task made offline"})
        edge.put(f"/api/tasks/{task}", json={"title": "replayed, renamed"})
    cluster.tunnel.open()
    cluster.wait_attached(timeout=30)
    _settled(cluster)
    envelopes = [row[0] for row in cluster.edge.sql("SELECT envelope FROM sync_outbox ORDER BY hlc")][-3:]
    tables = ("tasks", "task_memos", "sync_changes", "sync_op_receipts", "sync_conflicts")

    def snapshot() -> dict:
        counts = {t: cluster.center.sql(f'SELECT count(*) FROM "{t}"')[0][0] for t in tables}
        # The task's own events (a restart logs lifecycle events of its own).
        events = cluster.center.sql("SELECT count(*) FROM events WHERE data LIKE ?", f"%{task}%")[0][0]
        return {**counts, "events": events, "task": cluster.center.sql("SELECT * FROM tasks WHERE id = ?", task)}

    before = snapshot()
    assert before["task"] and before["task"][0][2] == "replayed, renamed"
    assert cluster.center.sql("SELECT count(*) FROM task_memos WHERE task_id = ?", task) == [(1,)]
    first_answers = None
    with httpx.Client(base_url=cluster.center.url, trust_env=False, timeout=30) as center:
        for _ in range(3):
            answers = [center.post("/api/sync/ops", content=envelope,
                                   headers={"content-type": "application/json"}).json() for envelope in envelopes]
            assert all(answer["replayed"] for answer in answers)
            first_answers = first_answers or answers
            assert answers == first_answers
    assert snapshot() == before
    cluster.center.stop()
    cluster.center.start()
    with httpx.Client(base_url=cluster.center.url, trust_env=False, timeout=30) as center:
        answer = center.post("/api/sync/ops", content=envelopes[0], headers={"content-type": "application/json"})
        assert answer.json()["replayed"]
    assert snapshot() == before


def test_a_forward_the_center_answers_too_late_takes_effect_once(cluster: Cluster) -> None:
    """Acceptance 7: the center's answer is held back 5 s; the edge times out and stages."""
    with cluster.edge.client() as edge:
        cluster.tunnel.delay = 5.0
        started = time.monotonic()
        response = _create_task(edge, cluster, "answered late")
        assert response.headers.get("x-aiteam-sync") == "staged"
        assert 2.5 < time.monotonic() - started < 5.0  # the edge gave up after its 3 s
        task = response.json()["data"]["id"]
        wait_for(lambda: cluster.center.sql("SELECT 1 FROM tasks WHERE id = ?", task), timeout=10,
                 what="the late forward to land on the center")
        cluster.tunnel.delay = 0.0
    cluster.wait_attached(timeout=30)
    _settled(cluster)
    for node in (cluster.center, cluster.edge):
        assert node.sql("SELECT id FROM tasks WHERE title = 'answered late'") == [(task,)]
    op = cluster.edge.sql("SELECT op_id, state FROM sync_outbox WHERE envelope LIKE '%answered late%'")
    assert len(op) == 1 and op[0][1] == "acked"
    assert cluster.center.sql("SELECT count(*) FROM sync_op_receipts WHERE op_id = ?", op[0][0]) == [(1,)]
    assert cluster.center.sql(
        "SELECT count(*) FROM events WHERE type = 'task.created' AND data LIKE '%answered late%'")[0][0] <= 1


def test_killing_either_api_mid_drain_loses_and_repeats_nothing(cluster: Cluster) -> None:
    """Acceptance 6: SIGKILL the edge's API, then the center's, while a backlog drains."""
    def queue(prefix: str, count: int) -> None:
        cluster.tunnel.cut()
        with cluster.edge.client() as edge:
            for n in range(count):
                _create_task(edge, cluster, f"{prefix} {n:02d}")

    def landed(prefix: str) -> int:
        return cluster.center.sql("SELECT count(*) FROM tasks WHERE title LIKE ?", f"{prefix} %")[0][0]

    queue("edge-killed", 30)
    cluster.tunnel.delay = 0.05  # slow the drain so the kill lands in the middle of it
    cluster.tunnel.open()
    wait_for(lambda: 0 < landed("edge-killed") < 30, timeout=30, what="the drain to be under way")
    cluster.edge.kill()
    cluster.edge.start()
    cluster.tunnel.delay = 0.0
    _settled(cluster)
    assert landed("edge-killed") == 30

    queue("center-killed", 30)
    cluster.tunnel.delay = 0.05
    cluster.tunnel.open()
    wait_for(lambda: 0 < landed("center-killed") < 30, timeout=30, what="the second drain to be under way")
    cluster.center.kill()
    cluster.wait_detached(timeout=30)
    cluster.center.start()  # same port: the tunnel still leads to it
    cluster.tunnel.delay = 0.0
    cluster.wait_attached(timeout=30)
    _settled(cluster)
    assert landed("center-killed") == 30
    titles = cluster.center.sql("SELECT title FROM tasks WHERE title LIKE '%-killed %'")
    assert len(titles) == len(set(titles)) == 60
    assert shared_face_differences(cluster.edge.db, cluster.center.db) == []


def test_a_shared_write_outside_any_operation_turns_the_health_check_red(cluster: Cluster) -> None:
    """Acceptance 8: none while a session works online; one written by hand is reported."""
    with cluster.edge.client() as edge:
        session = "orphan-check"
        for name in ("SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop"):
            event = {"hook_event_name": name, "session_id": session, "cwd": "/work/shared", "tool_name": "Bash",
                     "tool_input": {"command": "true"}, "tool_use_id": "tu-1", "prompt": "hi", "source": "startup"}
            assert edge.post("/api/hooks/event", json=event).status_code == 200
        task = _create_task(edge, cluster, "worked on").json()["data"]["id"]
        edge.put(f"/api/tasks/{task}/complete", json={"result": "done"})
        edge.get("/api/leader-briefings")
        edge.get(f"/api/projects/{cluster.project_id}/task-wall")
        assert edge.get("/api/sync/status").json()["orphan_writes"] == 0
        cluster.edge.write("UPDATE tasks SET title = 'written by hand' WHERE id = ?", task)
        status = edge.get("/api/sync/status").json()
    assert status["orphan_writes"] == 1 and not status["healthy"]
    assert any("孤儿写入" in problem for problem in status["problems"]), json.dumps(status, ensure_ascii=False)


def test_an_edge_behind_the_retention_rebuilds_its_replica_and_keeps_its_queue(cluster: Cluster) -> None:
    """Once the center dropped the tombstones an edge still needs, it rebuilds."""
    with cluster.edge.client() as edge, cluster.center.client() as center:
        doomed = _create_task(center, cluster, "deleted while apart").json()["data"]["id"]
        _settled(cluster)
        cluster.tunnel.cut()
        queued = _create_task(edge, cluster, "queued while apart").json()["data"]["id"]
        center.post(f"/api/projects/{cluster.project_id}/tasks", json={"title": "made while apart"})
        cluster.center.write("DELETE FROM tasks WHERE id = ?", doomed)
    # The center pruned everything the edge has not seen, tombstone included.
    high = cluster.center.sql("SELECT max(seq) FROM sync_changes")[0][0]
    cluster.center.write("DELETE FROM sync_changes WHERE seq < ?", high)
    cluster.center.write("UPDATE sync_state SET pruned_seq = ?", high - 1)
    cluster.tunnel.open()
    cluster.wait_attached(timeout=30)
    _settled(cluster)
    titles = {title for (title,) in cluster.edge.sql("SELECT title FROM tasks")}
    assert "made while apart" in titles and "deleted while apart" not in titles
    assert cluster.edge.sql("SELECT id FROM tasks WHERE title = 'queued while apart'") == [(queued,)]
    assert shared_face_differences(cluster.edge.db, cluster.center.db) == []


def _titles(node, pattern: str) -> int:
    return node.sql("SELECT count(*) FROM tasks WHERE title LIKE ?", pattern)[0][0]


def test_a_peer_that_stops_being_the_center_mid_drain_loses_nothing(cluster: Cluster) -> None:
    """B1: the center restarts without its identity (answers 409 as a standalone node)."""
    cluster.tunnel.cut()
    with cluster.edge.client() as edge:
        for n in range(12):
            _create_task(edge, cluster, f"kept {n:02d}")
    cluster.tunnel.delay = 0.05
    cluster.tunnel.open()
    wait_for(lambda: 0 < _titles(cluster.center, "kept %") < 12, timeout=30, what="the drain to be under way")
    cluster.center.kill()
    identity = cluster.center.data / "node.json"
    identity.rename(identity.with_suffix(".away"))
    cluster.center.start()  # same port, standalone now
    cluster.tunnel.delay = 0.0
    cluster.wait_detached(timeout=30)
    states = {state for (state,) in cluster.edge.sql("SELECT state FROM sync_outbox WHERE envelope LIKE '%kept%'")}
    assert "dropped" not in states and "pending" in states
    assert _titles(cluster.edge, "kept %") == 12  # nothing staged was settled away
    with cluster.edge.client() as edge:
        status = edge.get("/api/sync/status").json()
    assert not status["healthy"] and status["pending_ops"] > 0
    cluster.center.stop()
    # Standalone, the center stamped what it wrote with no origin, like the baseline's
    # rows: its own request-ledger events. Nothing of the edge's, and only these.
    while_standalone = cluster.center.sql("SELECT id, type FROM events WHERE origin_node = '' AND type != ? "
                                          "AND timestamp > (SELECT max(timestamp) FROM events WHERE type = ?)",
                                          "cc.tool_use", "cc.tool_use")
    assert {kind for _, kind in while_standalone} <= {"api.request_rollup"}
    cluster.tolerated.update(f"events {event_id}: 只在中心" for event_id, _ in while_standalone)
    identity.with_suffix(".away").rename(identity)
    cluster.center.start()
    cluster.wait_attached(timeout=30)
    _settled(cluster)
    assert _titles(cluster.center, "kept %") == _titles(cluster.edge, "kept %") == 12
    assert shared_face_differences(cluster.edge.db, cluster.center.db) == []


def test_a_completed_task_is_not_reopened_by_writes_that_never_saw_it(cluster: Cluster) -> None:
    """B2: the edge's second offline write counts the same revision as the center's completion."""
    with cluster.edge.client() as edge, cluster.center.client() as center:
        task = _create_task(edge, cluster, "finished on mini").json()["data"]["id"]
        _settled(cluster)
        cluster.tunnel.cut()
        assert center.put(f"/api/tasks/{task}", json={"status": "completed", "result": "done on mini"}) \
            .status_code == 200
        assert edge.put(f"/api/tasks/{task}", json={"status": "running"}).headers.get("x-aiteam-sync") == "staged"
        assert edge.put(f"/api/tasks/{task}", json={"status": "pending", "assigned_to": None}).status_code == 200
        cluster.tunnel.open()
        cluster.wait_attached(timeout=30)
        _settled(cluster)
        lost = [(c["field"], c["loser"]) for c in center.get("/api/sync/conflicts").json()["items"]
                if c["pk"] == task and c["field"] == "status"]
    for node in (cluster.center, cluster.edge):
        assert node.sql("SELECT status, result FROM tasks WHERE id = ?", task) == [("completed", "done on mini")]
    assert sorted(lost) == [("status", "pending"), ("status", "running")]


def test_the_later_of_two_offline_edits_of_one_field_stands(cluster: Cluster) -> None:
    """The center stamps a replayed field with the operation's clock, not the replay's."""
    with cluster.edge.client() as edge:
        task = _create_task(edge, cluster, "renamed twice").json()["data"]["id"]
        _settled(cluster)
        cluster.tunnel.cut()
        edge.put(f"/api/tasks/{task}", json={"title": "first offline"})
        edge.put(f"/api/tasks/{task}", json={"title": "second offline"})
    cluster.tunnel.open()
    cluster.wait_attached(timeout=30)
    _settled(cluster)
    assert cluster.center.sql("SELECT title FROM tasks WHERE id = ?", task) == [("second offline",)]


def test_a_staged_write_the_center_refuses_leaves_no_ghost_row(cluster: Cluster) -> None:
    """A task staged offline in a project deleted on the center meanwhile: a pending conflict,
    and the edge's staged row is gone once the center's verdict is in."""
    with cluster.edge.client() as edge, cluster.center.client() as center:
        doomed = edge.post("/api/projects", json={"name": "doomed", "root_path": "/work/doomed"}).json()["data"]["id"]
        _settled(cluster)
        cluster.tunnel.cut()
        assert center.delete(f"/api/projects/{doomed}").status_code == 200
        ghost = edge.post(f"/api/projects/{doomed}/tasks", json={"title": "ghost"})
        assert ghost.status_code == 201 and ghost.headers.get("x-aiteam-sync") == "staged"
        cluster.tunnel.open()
        cluster.wait_attached(timeout=30)
        _settled(cluster)
        pending = [c for c in center.get("/api/sync/conflicts", params={"status": "pending"}).json()["items"]
                   if c["loser"] and c["loser"].get("title") == "ghost"]
    assert len(pending) == 1  # the write is kept, for the user
    assert _titles(cluster.edge, "ghost") == _titles(cluster.center, "ghost") == 0
    assert shared_face_differences(cluster.edge.db, cluster.center.db) == []


def test_preferring_a_node_flips_its_latest_batch_and_leaves_later_writes_alone(cluster: Cluster) -> None:
    """S2: only the batch asked for, and never a field written again since its decision."""
    def lose_title(edge: httpx.Client, center: httpx.Client, task: str, ours: str, theirs: str) -> None:
        cluster.tunnel.cut()
        edge.put(f"/api/tasks/{task}", json={"title": ours})
        time.sleep(0.01)  # the center's write is the later one
        center.put(f"/api/tasks/{task}", json={"title": theirs})
        cluster.tunnel.open()
        cluster.wait_attached(timeout=30)
        _settled(cluster)

    with cluster.edge.client() as edge, cluster.center.client() as center:
        first = _create_task(edge, cluster, "first").json()["data"]["id"]
        second = _create_task(edge, cluster, "second").json()["data"]["id"]
        _settled(cluster)
        lose_title(edge, center, first, "edge 1", "center 1")
        batch_one = cluster.center.sql("SELECT batch FROM sync_conflicts WHERE pk = ?", first)[0][0]
        center.put(f"/api/tasks/{first}", json={"title": "final, agreed by everyone"})
        lose_title(edge, center, second, "edge 2", "center 2")
        flipped = center.post("/api/sync/conflicts/prefer", json={"node_id": EDGE_ID}).json()["items"]
        assert [(c["pk"], c["status"]) for c in flipped] == [(second, "flipped")]
        again = center.post("/api/sync/conflicts/prefer", json={"node_id": EDGE_ID, "batch": batch_one}).json()
        assert [(c["pk"], c["status"]) for c in again["items"]] == [(first, "superseded")]
    titles = dict(cluster.center.sql("SELECT id, title FROM tasks WHERE id IN (?, ?)", first, second))
    assert titles == {first: "final, agreed by everyone", second: "edge 2"}


def test_an_online_only_write_is_read_back_at_once(cluster: Cluster) -> None:
    """S3: deletions wait for the replica like any forwarded write."""
    with cluster.edge.client() as edge:
        for n in range(5):
            report = edge.post("/api/reports", json={"author": "x", "topic": f"gone-{n}", "content": "c"},
                               headers=_scope(cluster)).json()["id"]
            assert edge.delete(f"/api/reports/{report}").status_code == 200
            assert edge.get(f"/api/reports/{report}").status_code == 404


def test_concurrent_writes_while_online_are_forwarded_not_staged(cluster: Cluster) -> None:
    """S4: requests in flight together are concurrent, not a queue for each other."""
    from concurrent.futures import ThreadPoolExecutor

    def create(n: int) -> httpx.Response:
        with cluster.edge.client(timeout=60) as edge:
            return _create_task(edge, cluster, f"together {n:02d}")

    with ThreadPoolExecutor(max_workers=32) as pool:
        answers = list(pool.map(create, range(32)))
    assert [a.headers.get("x-aiteam-sync") for a in answers].count("staged") == 0
    assert _titles(cluster.center, "together %") == 32


@pytest.fixture()
def governed(tmp_path: Path):
    nodes = Cluster(tmp_path)
    try:
        nodes.cut_over(SYNC_TEST_WATCHDOG="0.5")
        yield nodes
        nodes.assert_one_writer_per_origin()
    finally:
        nodes.close()


def test_an_edge_resets_its_stuck_agents_task_through_the_center(governed: Cluster) -> None:
    """The edge's own watchdog, running: the agent is reset here, the task on the center."""
    cluster = governed
    with cluster.edge.client() as edge:
        answer = edge.post(f"/api/teams/{cluster.team_id}/tasks/run",
                           json={"title": "left running", "description": "d", "assigned_to": "stuck-agent"})
        assert answer.status_code == 200, answer.text
        made = answer.json()["data"]["id"]
        assert edge.put(f"/api/tasks/{made}", json={"status": "running"}).status_code == 200
    wait_for(lambda: cluster.edge.sql("SELECT status FROM tasks WHERE id = ?", made) == [("running",)],
             what="the running task in the edge's replica")
    # Only now the agent, busy for months: the patrol must find its task running.
    long_ago = "2026-01-01 00:00:00.000000"
    cluster.edge.write(
        "INSERT INTO agents (id, team_id, name, role, system_prompt, model, status, config, source, trust_score, "
        "created_at, last_active_at, origin_node) VALUES ('stuck-agent', ?, 'stuck', 'member', '', '', 'busy', '{}', "
        "'api', 0.5, ?, ?, ?)", cluster.team_id, long_ago, long_ago, EDGE_ID)
    wait_for(lambda: cluster.center.sql("SELECT status, assigned_to FROM tasks WHERE id = ?", made)
             == [("pending", None)], timeout=20, what="the reset to reach the center")
    assert cluster.edge.sql("SELECT status FROM agents WHERE id = 'stuck-agent'") == [("waiting",)]
    wait_for(lambda: ("watchdog",) in cluster.center.sql("SELECT author FROM task_memos WHERE task_id = ?", made),
             timeout=10, what="the recovery note on the center")
    assert cluster.edge.sql("SELECT count(*) FROM sync_changes WHERE table_name = 'tasks'") == [(0,)]
