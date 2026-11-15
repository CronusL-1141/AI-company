"""Guards on the write paths of both node roles, each read back from the database.

* An edge forwards a write only when nothing queued is ahead of it; operations of
  requests it is handling at the same moment are concurrent, not a queue.
* An edge leaves expiring briefings to the center: a read must not write the
  shared face on a replica.
* The center runs a slow online-only operation without holding up the others, and
  an operation arriving twice at once still runs once.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI

from aiteam.clock import utc_now
from aiteam.ids import time_ordered_id
from aiteam.storage.connection import close_db
from aiteam.storage.repository import StorageRepository
from aiteam.sync import context as sync_context
from aiteam.sync import hlc, ledger
from aiteam.sync.edge import EdgeSync
from aiteam.sync.operations import Operations
from aiteam.sync.protocol import SYNC_PROTOCOL
from aiteam.sync.surface import route_surface
from aiteam.types import OpOutcome, RouteSurface, SyncOperation


@pytest.fixture()
async def repo(tmp_path: Path):
    repository = StorageRepository(db_url=f"sqlite+aiosqlite:///{tmp_path / 'node.db'}")
    await repository.init_db()
    yield repository, tmp_path / "node.db"
    await close_db()


def _op(path: str, body: dict | None = None, op_id: str = "") -> SyncOperation:
    clock = hlc.encode(hlc.Hlc(hlc.now_ms(), 0, "air-3f9c"))
    return SyncOperation(op_id=op_id or time_ordered_id(), origin_node="air-3f9c", hlc=clock, method="POST",
                         path=path, body=json.dumps(body or {}), headers={"content-type": "application/json"},
                         sync_protocol=SYNC_PROTOCOL)


async def test_an_edge_never_forwards_past_a_queued_operation(repo) -> None:
    repository, _ = repo
    edge = EdgeSync(app=None, db_url=repository.db_url, node_id="air-3f9c", center_url="http://center",
                    db_path="")
    try:
        await edge._ledger(ledger.ensure_peer, write=True)
        await edge._ledger(lambda conn: conn.exec_driver_sql("UPDATE sync_peers SET link = 'attached'"), write=True)
        earlier = await edge._ledger(ledger.record, _op("/api/reports"), write=True)
        later = await edge._ledger(ledger.record, _op("/api/reports"), write=True)
        assert not await edge._may_forward(later)  # the earlier one is queued
        edge._in_flight.add(earlier.op_id)
        assert await edge._may_forward(later)  # another request is forwarding it right now
        await edge._ledger(ledger.detach, "gone", write=True)
        assert not await edge._may_forward(later)
    finally:
        await edge.stop()


async def test_an_edge_leaves_briefing_expiry_to_the_center(repo) -> None:
    from aiteam.services.notices.detectors.decisions import EXPIRY, expire_stale

    repository, path = repo
    briefing = await repository.create_briefing(title="old question")
    con = sqlite3.connect(path)
    con.execute("UPDATE leader_briefings SET created_at = ?",
                ((utc_now() - EXPIRY - timedelta(days=1)).isoformat(),))
    con.commit()
    con.close()
    sync_context.set_process_node("air-3f9c", "edge")
    try:
        assert await expire_stale(repository, utc_now(), force=True) == 0
    finally:
        sync_context.set_process_node("", "standalone")
    con = sqlite3.connect(path)
    assert con.execute("SELECT status FROM leader_briefings WHERE id = ?", (briefing.id,)).fetchone() == ("pending",)
    con.close()
    assert await expire_stale(repository, utc_now(), force=True) == 1  # standalone (and the center) expire it


def _center_app(release: asyncio.Event, calls: list[str]) -> FastAPI:
    app = FastAPI()

    @app.post("/slow")
    @route_surface(RouteSurface.shared(offline_ok=False))
    async def slow() -> dict:
        calls.append("slow")
        await release.wait()
        return {"success": True}

    @app.post("/fast", status_code=201)
    @route_surface(RouteSurface.shared(offline_ok=True))
    async def fast() -> dict:
        calls.append("fast")
        return {"success": True}

    return app


async def test_a_slow_online_write_holds_up_no_other_operation(repo) -> None:
    repository, path = repo
    release, calls = asyncio.Event(), []
    operations = Operations(_center_app(release, calls), repository, node_id="mini-c0de")
    slow_op = _op("/slow")
    slow = asyncio.create_task(operations.apply(slow_op))
    again = asyncio.create_task(operations.apply(slow_op))  # the same operation, arriving twice
    await asyncio.sleep(0)
    fast = await asyncio.wait_for(operations.apply(_op("/fast")), timeout=5)
    assert fast.outcome is OpOutcome.APPLIED and not slow.done()
    release.set()
    first, second = await slow, await again
    assert calls.count("slow") == 1 and second.replayed and not first.replayed
    con = sqlite3.connect(path)
    assert con.execute("SELECT count(*) FROM sync_op_receipts").fetchone() == (2,)
    con.close()


async def test_an_online_only_write_does_not_overtake_the_queue(repo) -> None:
    repository, _ = repo
    edge = EdgeSync(app=None, db_url=repository.db_url, node_id="air-3f9c", center_url="http://127.0.0.1:9",
                    db_path="")
    try:
        await edge._ledger(ledger.ensure_peer, write=True)
        await edge._ledger(lambda conn: conn.exec_driver_sql("UPDATE sync_peers SET link = 'attached'"), write=True)
        await edge._ledger(ledger.record, _op("/api/reports"), write=True)
        status, _, body = await edge._online_only(_op("/api/reports/r1"))
        assert status == 503 and "排队" in json.loads(body)["detail"]
    finally:
        await edge.stop()


async def test_a_queue_that_cannot_drain_turns_the_edge_red(repo) -> None:
    """S1: a head that keeps failing, or a backlog older than the alert, needs a person."""
    from aiteam.sync import status as sync_status
    from aiteam.types import NodeIdentity, NodeRole, NodeStatus

    repository, path = repo
    node = NodeStatus(identity=NodeIdentity(node_id="air-3f9c", role=NodeRole.EDGE), role=NodeRole.EDGE)
    edge = EdgeSync(app=None, db_url=repository.db_url, node_id="air-3f9c", center_url="http://center", db_path="")
    try:
        await edge._ledger(ledger.ensure_peer, write=True)
        op = await edge._ledger(ledger.record, _op("/api/reports"), write=True)
        healthy = await sync_status.read(node, repository.db_url, None)
        assert healthy.pending_ops == 1 and healthy.healthy
        for _ in range(3):
            await edge._ledger(ledger.attempt_failed, op.op_id, "中心暂时无法执行（503）", write=True)
        stuck = await sync_status.read(node, repository.db_url, None)
        assert not stuck.healthy and stuck.head_attempts == 3 and stuck.last_error.startswith("中心暂时无法执行")
        assert any("重试 3 次" in problem for problem in stuck.problems)
        con = sqlite3.connect(path)
        con.execute("UPDATE sync_outbox SET attempts = 0, created_at = ?",
                    ((utc_now() - timedelta(hours=1)).isoformat(),))
        con.commit()
        con.close()
        old = await sync_status.read(node, repository.db_url, None)
        assert not old.healthy and any("积压超过" in problem for problem in old.problems)
    finally:
        await edge.stop()
