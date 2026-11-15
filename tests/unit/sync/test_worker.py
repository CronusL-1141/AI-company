"""The edge worker's bookkeeping (aiteam.sync.worker), against a scripted center.

The center is an httpx MockTransport answering with the protocol's own models
(SyncHello, SyncOpResult, SyncChangesPage), so what the worker parses is what a
real center sends. Everything is read back through fresh sqlite3 connections.

* The link: two failed probes detach, three good ones start draining; a handshake
  that does not match (role, pinned center, protocol, clock convention, schema, a
  change log that went back or was replaced) keeps the edge detached.
* Only the center's verdict settles an operation; any other answer keeps it queued
  and detaches the edge at once. A head that fails for now does not stop the pull.
* Staged rows are replaced by the center's only once the center has a verdict on
  every operation that staged them, and an operation staged meanwhile keeps its own.
* Applying changes skips staged rows, never moves the cursor back, keeps per-node
  columns; an idle worker commits nothing.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from aiteam import __version__
from aiteam.ids import time_ordered_id
from aiteam.storage.connection import close_db
from aiteam.storage.repository import StorageRepository
from aiteam.sync import ledger
from aiteam.sync.protocol import SYNC_PROTOCOL, schema_version
from aiteam.sync.worker import Replay, Worker, open_engine
from aiteam.types import (
    LinkState,
    NodeRole,
    OpOutcome,
    SyncChange,
    SyncChangesPage,
    SyncHello,
    SyncOperation,
    SyncOpResult,
)

Handler = Callable[[httpx.Request], httpx.Response]


@pytest.fixture()
def edge(tmp_path: Path):
    path = tmp_path / "edge.db"

    async def build() -> None:
        await StorageRepository(db_url=f"sqlite+aiosqlite:///{path}").init_db()
        await close_db()

    asyncio.run(build())
    engine = open_engine(str(path))
    with engine.begin() as conn:
        conn.exec_driver_sql("UPDATE sync_state SET capture = 1, node_id = 'air-3f9c'")
        ledger.ensure_peer(conn)
    yield engine, path
    engine.dispose()


def _worker(edge, handler: Handler | None = None) -> Worker:
    engine, _ = edge
    client = httpx.Client(transport=httpx.MockTransport(handler or (lambda request: httpx.Response(599))),
                          base_url="http://center")
    return Worker(engine, client, node_id="air-3f9c")


def _sql(path: Path, statement: str, *params) -> list[tuple]:
    con = sqlite3.connect(path)
    try:
        rows = con.execute(statement, params).fetchall()
        con.commit()
        return rows
    finally:
        con.close()


def _hello(path: Path, **override) -> SyncHello:
    version = _sql(path, "PRAGMA user_version")[0][0]
    fields = dict(node_id="mini-c0de", role=NodeRole.CENTER, sync_protocol=SYNC_PROTOCOL, user_version=version,
                  schema_version=schema_version(), api_version=__version__, baseline_seq=5, max_seq=9)
    return SyncHello(**{**fields, **override})


def _queue(edge, title: str, *, staged_pk: str = "") -> str:
    engine, _ = edge
    op = SyncOperation(op_id=time_ordered_id(), origin_node="air-3f9c", hlc="",
                       method="POST", path="/api/projects/p/tasks", body=json.dumps({"title": title}),
                       sync_protocol=SYNC_PROTOCOL)
    with engine.begin() as conn:
        op = ledger.record(conn, op)
        if staged_pk:
            conn.exec_driver_sql("INSERT INTO sync_tentative (table_name, pk, op_id) VALUES ('projects', ?, ?)",
                                 (staged_pk, op.op_id))
    return op.op_id


class _Commits:
    """Counts commits by other connections: PRAGMA data_version, read on one connection that stays open."""

    def __init__(self, path: Path) -> None:
        self._con = sqlite3.connect(path)
        self._start = self._now()

    def _now(self) -> int:
        return self._con.execute("PRAGMA data_version").fetchone()[0]

    def since(self) -> int:
        try:
            return self._now() - self._start
        finally:
            self._con.close()


def test_the_link_follows_the_probes(edge) -> None:
    worker, (_, path) = _worker(edge), edge
    hello = _hello(path)
    states = [worker.record_probe(hello, worker.handshake_problem(hello, worker.peer())) for _ in range(3)]
    assert states == [LinkState.DETACHED, LinkState.DETACHED, LinkState.DRAINING]
    assert _sql(path, "SELECT center_node_id, cursor FROM sync_peers") == [("mini-c0de", 5)]  # pinned, baseline
    _sql(path, "UPDATE sync_peers SET link = 'attached'")
    assert worker.record_probe(None, "refused") is LinkState.ATTACHED  # one failure is not enough
    assert worker.record_probe(None, "refused") is LinkState.DETACHED


@pytest.mark.parametrize(("override", "fragment"), [
    ({"role": NodeRole.EDGE}, "不是中心"),
    ({"node_id": "other-c0de"}, "中心身份变了"),
    ({"sync_protocol": SYNC_PROTOCOL + 1}, "同步协议不符"),
    ({"user_version": 1}, "时钟制式不符"),
    ({"schema_version": "0" * 16}, "同步表结构不符"),
    ({"max_seq": 2}, "变更日志与本机记下的不符"),
    ({"cursor_known": False}, "变更日志与本机记下的不符"),
], ids=["role", "pinned", "protocol", "clock", "schema", "regressed", "replaced"])
def test_a_handshake_that_does_not_match_keeps_the_edge_detached(edge, override, fragment) -> None:
    worker, (_, path) = _worker(edge), edge
    _sql(path, "UPDATE sync_peers SET center_node_id = 'mini-c0de', cursor = 4, link = 'attached'")
    hello = _hello(path, **override)
    problem = worker.handshake_problem(hello, worker.peer())
    assert fragment in problem
    assert worker.record_probe(hello, problem) is LinkState.DETACHED
    assert _sql(path, "SELECT handshake_error FROM sync_peers")[0][0] == problem


def test_the_probe_asks_whether_the_change_at_the_cursor_is_still_there(edge) -> None:
    seen: list[httpx.Request] = []

    def center(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=_hello(edge[1], max_seq=20, cursor_known=True).model_dump_json())

    _sql(edge[1], "UPDATE sync_peers SET cursor = 7, cursor_hlc = 'h7'")
    hello, problem = _worker(edge, center).probe()
    assert problem == "" and hello.cursor_known
    assert dict(seen[0].url.params) == {"cursor": "7", "cursor_hlc": "h7"}


@pytest.mark.parametrize("status", [409, 404, 400, 422], ids=["not-a-center", "old-code", "refused", "invalid"])
def test_an_answer_that_is_no_verdict_keeps_the_queue_and_detaches(edge, status) -> None:
    engine, path = edge
    _sql(path, "UPDATE sync_peers SET link = 'attached'")
    first, second = _queue(edge, "one", staged_pk="t1"), _queue(edge, "two!")
    worker = _worker(edge, lambda request: httpx.Response(status, json={"detail": "nope"}))
    assert worker.replay() is Replay.DETACHED
    assert sorted(_sql(path, "SELECT op_id, state, attempts FROM sync_outbox")) == sorted(
        [(first, "pending", 1), (second, "pending", 0)])
    assert _sql(path, "SELECT link FROM sync_peers") == [("detached",)]
    assert f"（{status}）" in _sql(path, "SELECT last_error FROM sync_peers")[0][0]
    worker.reconcile()  # no verdict, no settling: the staged row stays
    assert _sql(path, "SELECT pk FROM sync_tentative") == [("t1",)]


def test_a_verdict_settles_and_a_temporary_failure_waits_at_the_head(edge) -> None:
    engine, path = edge
    first, second = _queue(edge, "one"), _queue(edge, "two!")
    answers = iter([httpx.Response(200, content=SyncOpResult(op_id=first, outcome=OpOutcome.CONFLICT,
                                                             status_code=404, ack_seq=3).model_dump_json()),
                    httpx.Response(503, json={"detail": "busy"})])
    worker = _worker(edge, lambda request: next(answers))
    assert worker.replay() is Replay.WAITING
    assert dict((op, (state, ack)) for op, state, ack in _sql(path, "SELECT op_id, state, ack_seq FROM sync_outbox")) \
        == {first: ("conflict", 3), second: ("pending", None)}


def test_a_stuck_head_does_not_stop_the_pull(edge) -> None:
    engine, path = edge
    _sql(path, "UPDATE sync_peers SET link = 'attached', center_node_id = 'mini-c0de'")
    _queue(edge, "stuck")
    row = {"id": "p9", "name": "from center", "root_path": "/w/p9", "description": "", "config": "{}",
           "created_at": "2026-01-01", "updated_at": "2026-01-01", "origin_node": "mini-c0de"}

    def center(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/sync/ops":
            return httpx.Response(503, json={"detail": "clock ahead"})
        if request.url.path == "/api/sync/changes":
            return httpx.Response(200, content=SyncChangesPage(
                changes=[SyncChange(seq=4, table_name="projects", pk="p9", row=row)], upto=4, upto_hlc="h4",
                max_seq=4).model_dump_json())
        return httpx.Response(200, json={"rows": []})

    _worker(edge, center).cycle(probe=False)
    assert _sql(path, "SELECT name FROM projects WHERE id = 'p9'") == [("from center",)]
    assert _sql(path, "SELECT cursor, cursor_hlc FROM sync_peers") == [(4, "h4")]
    assert _sql(path, "SELECT state, attempts FROM sync_outbox") == [("pending", 1)]


def test_settling_waits_for_a_verdict_and_spares_what_was_staged_since(edge) -> None:
    engine, path = edge
    _sql(path, "UPDATE sync_peers SET cursor = 10")
    _sql(path, "INSERT INTO projects (id, name, root_path, description, config, created_at, updated_at) "
               "VALUES ('t1', 'staged', '/w/t1', '', '{}', '2026-01-01', '2026-01-01')")
    acked = _queue(edge, "acked", staged_pk="t1")
    _sql(path, "UPDATE sync_outbox SET state = 'acked', ack_seq = 9 WHERE op_id = ?", acked)
    later: list[str] = []

    def center(request: httpx.Request) -> httpx.Response:
        # The rows are fetched; meanwhile another operation stages the same row.
        later.append(_queue(edge, "staged meanwhile", staged_pk="t1"))
        return httpx.Response(200, json={"rows": [{"seq": 0, "table_name": "projects", "pk": "t1", "row": None}]})

    worker = _worker(edge, center)
    worker.reconcile()
    # The row was staged again meanwhile: it and both registrations wait for the newer operation.
    assert sorted(_sql(path, "SELECT op_id FROM sync_tentative")) == sorted([(acked,), (later[0],)])
    assert _sql(path, "SELECT name FROM projects WHERE id = 't1'") == [("staged",)]
    _sql(path, "UPDATE sync_outbox SET state = 'acked', ack_seq = 10 WHERE op_id = ?", later[0])
    worker.client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(
        200, json={"rows": [{"seq": 0, "table_name": "projects", "pk": "t1", "row": None}]})),
        base_url="http://center")
    worker.reconcile()  # both settled: the center has no such row, so neither has the replica
    assert _sql(path, "SELECT count(*) FROM sync_tentative") == [(0,)]
    assert _sql(path, "SELECT count(*) FROM projects WHERE id = 't1'") == [(0,)]


def test_an_idle_worker_commits_nothing(edge) -> None:
    engine, path = edge
    _sql(path, "UPDATE sync_peers SET link = 'attached', center_node_id = 'mini-c0de', cursor = 4, "
               "successes = 3, last_ok_at = ?", "2999-01-01T00:00:00+00:00")

    def center(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/sync/hello":
            return httpx.Response(200, content=_hello(path, max_seq=4).model_dump_json())
        return httpx.Response(200, content=SyncChangesPage(upto=4, max_seq=4).model_dump_json())

    worker = _worker(edge, center)
    commits = _Commits(path)
    for probe in (True, False, False, True):
        worker.cycle(probe=probe)
    assert commits.since() == 0


def _project(project_id: str, root: str, name: str) -> dict:
    return {"id": project_id, "name": name, "root_path": root, "description": "", "config": "{}",
            "created_at": "2026-01-01", "updated_at": "2026-01-01", "origin_node": "mini-c0de"}


def test_applying_changes_keeps_staged_rows_and_never_moves_the_cursor_back(edge) -> None:
    worker, (_, path) = _worker(edge), edge
    _sql(path, "INSERT INTO projects (id, name, root_path, description, config, created_at, updated_at) "
               "VALUES ('local', 'staged here', '/work/x', '', '{}', '2026-01-01', '2026-01-01')")
    _sql(path, "INSERT INTO projects (id, name, root_path, description, config, created_at, updated_at) "
               "VALUES ('mine', 'staged', '/work/mine', '', '{}', '2026-01-01', '2026-01-01')")
    _sql(path, "INSERT INTO sync_tentative (table_name, pk, op_id) VALUES ('projects', 'mine', 'op')")
    logged = _sql(path, "SELECT count(*) FROM sync_changes")
    page = SyncChangesPage(changes=[
        SyncChange(seq=3, table_name="projects", pk="center", row=_project("center", "/work/x", "from center")),
        SyncChange(seq=4, table_name="projects", pk="mine", row=_project("mine", "/work/mine", "center's view")),
    ], upto=6, upto_hlc="h6", max_seq=6)
    worker.apply(page)
    rows = dict(_sql(path, "SELECT id, name FROM projects"))
    # The center's row took /work/x from the local one; the staged row waits for its operation.
    assert rows == {"center": "from center", "mine": "staged"}
    assert _sql(path, "SELECT cursor, cursor_hlc FROM sync_peers") == [(6, "h6")]
    assert _sql(path, "SELECT count(*) FROM sync_changes") == logged  # suppressed: no orphan, no echo
    worker.apply(SyncChangesPage(changes=[
        SyncChange(seq=2, table_name="projects", pk="center", row=_project("center", "/work/x", "older"))],
        upto=2, max_seq=6))
    assert dict(_sql(path, "SELECT id, name FROM projects"))["center"] == "from center"
    assert _sql(path, "SELECT cursor FROM sync_peers") == [(6,)]
    worker.apply(SyncChangesPage(changes=[SyncChange(seq=7, table_name="projects", pk="center", row=None)],
                                 upto=7, max_seq=7))
    assert "center" not in dict(_sql(path, "SELECT id, name FROM projects"))
    # Every id that left the replica is remembered, the evicted local one too.
    assert sorted(_sql(path, "SELECT pk FROM sync_tombstones")) == [("center",), ("local",)]


def test_columns_that_belong_to_one_node_keep_their_local_values(edge) -> None:
    worker, (_, path) = _worker(edge), edge
    _sql(path, "INSERT INTO memories (id, scope, scope_id, content, kind, metadata, source_refs, created_at, "
               "accessed_at) VALUES ('m', 'global', 'system', 'old', 'preference', '{}', '[]', '2026-01-01', "
               "'2026-03-03')")
    link_key = json.dumps(["task", "t", "report", "r", "cites"], separators=(",", ":"))
    link = {"id": 99, "from_kind": "task", "from_id": "t", "to_kind": "report", "to_id": "r", "link_type": "cites",
            "link_source": "auto", "context": "{}", "project_id": "", "created_at": "2026-01-01",
            "origin_node": "mini-c0de"}
    memory = {"id": "m", "scope": "global", "scope_id": "system", "content": "new", "kind": "preference",
              "metadata": "{}", "source_refs": "[]", "created_at": "2026-01-01", "accessed_at": "2026-01-01",
              "origin_node": "mini-c0de"}
    worker.apply(SyncChangesPage(changes=[
        SyncChange(seq=1, table_name="memories", pk="m", row=memory),
        SyncChange(seq=2, table_name="knowledge_links", pk=link_key, row=link)], upto=2, max_seq=2))
    assert _sql(path, "SELECT content, accessed_at FROM memories") == [("new", "2026-03-03")]
    assert _sql(path, "SELECT id != 99, link_type FROM knowledge_links") == [(1, "cites")]
