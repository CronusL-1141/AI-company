"""One writer per origin: the center hands an edge's session writes back, and refuses one that slips through.

* While the center carries out an edge's operation, an agent of that edge is not
  changed there: the change goes back in the verdict; an agent of the center is
  changed as always; one of a third node is left alone.
* The edge writes the effects when it settles: events always, row updates only for
  an operation it did not stage (staging made them already).
* A route that writes the edge's session face on the center anyway makes the
  operation fail (503, rolled back): nothing of it stays, the edge retries.
Read back through fresh connections.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from fastapi import FastAPI
from sqlalchemy import create_engine

from aiteam.ids import time_ordered_id
from aiteam.storage.connection import close_db, get_session
from aiteam.storage.repository import StorageRepository
from aiteam.sync import context as sync_context
from aiteam.sync import effects, hlc, ledger
from aiteam.sync.intake import Intake
from aiteam.sync.operations import Operations, TemporaryFailureError
from aiteam.sync.protocol import SYNC_PROTOCOL
from aiteam.sync.surface import route_surface
from aiteam.types import OpOutcome, RouteSurface, SyncEffectKind, SyncOperation, SyncOpResult

CENTER, EDGE, OTHER = "mini-c0de", "air-ed9e", "pro-0b0e"


@pytest.fixture()
async def node(tmp_path: Path):
    path = tmp_path / "node.db"
    repository = StorageRepository(db_url=f"sqlite+aiosqlite:///{path}")
    await repository.init_db()
    yield repository, path
    sync_context.set_process_node("", "standalone")
    sync_context.set_baseline_owner("")
    await close_db()


def _sql(path: Path, query: str, *params) -> list[tuple]:
    con = sqlite3.connect(path)
    try:
        rows = con.execute(query, params).fetchall()
        con.commit()
        return rows
    finally:
        con.close()


def _agent(path: Path, agent_id: str, origin: str) -> None:
    _sql(path, "INSERT INTO agents (id, team_id, name, role, system_prompt, model, status, config, source, "
               "trust_score, created_at, origin_node) VALUES (?, 't', ?, 'worker', '', '', 'waiting', '{}', 'api', "
               "0.5, '2026-11-15 08:00:00', ?)", agent_id, agent_id, origin)


def _as_center(path: Path) -> None:
    sync_context.set_process_node(CENTER, "center", own_origins=(CENTER,))
    _sql(path, f"UPDATE sync_state SET capture = 1, node_id = '{CENTER}'")


async def test_an_agent_is_changed_only_by_its_own_node(node) -> None:
    repository, path = node
    _as_center(path)
    for agent_id, origin in (("a-edge", EDGE), ("a-center", CENTER), ("a-other", OTHER)):
        _agent(path, agent_id, origin)
    with sync_context.operation(time_ordered_id(), origin=EDGE, staged=False) as current:
        for agent_id in ("a-edge", "a-center", "a-other"):
            changed = await repository.update_agent(agent_id, current_task="write the report")
            assert changed.current_task == "write the report"
    assert sorted(_sql(path, "SELECT id, current_task FROM agents")) == [
        ("a-center", "write the report"), ("a-edge", None), ("a-other", None)]
    handed = [(e.kind, e.table_name, e.pk, e.fields) for e in current.effects if e.kind is SyncEffectKind.ROW_UPDATE]
    assert handed == [(SyncEffectKind.ROW_UPDATE, "agents", "a-edge", {"current_task": "write the report"})]
    events = [e.row["entity_id"] for e in current.effects if e.kind is SyncEffectKind.EVENT]
    assert events == ["a-edge", "a-center", "a-other"]  # every agent.updated goes back with the verdict
    assert _sql(path, "SELECT count(*) FROM events") == [(0,)]


async def test_an_edge_writes_its_effects_once_when_it_settles(node) -> None:
    repository, path = node
    _agent(path, "a-edge", EDGE)
    engine = create_engine(f"sqlite:///{path}")
    with engine.begin() as conn:
        ledger.ensure_peer(conn)
        ops = [ledger.record(conn, _op(f"/{n}")) for n in range(2)]
    conn_staged = "UPDATE sync_outbox SET staged = 1 WHERE op_id = ?"
    _sql(path, conn_staged, ops[1].op_id)
    with sync_context.operation(ops[0].op_id, origin=EDGE, staged=False) as current:
        sync_context.set_process_node(CENTER, "center", own_origins=(CENTER,))
        await repository.create_event("task.created", "test", {"n": 1}, entity_id="t1")
        await repository.update_agent("a-edge", current_task="from the center")
    sync_context.set_process_node(EDGE, "edge", own_origins=("", EDGE))
    verdict = SyncOpResult(op_id=ops[0].op_id, outcome=OpOutcome.APPLIED, status_code=200,
                           effects=current.effects)
    try:
        with engine.begin() as conn:
            ledger.settle(conn, ops[0].op_id, verdict)
        assert _sql(path, "SELECT current_task FROM agents") == [("from the center",)]
        assert _sql(path, "SELECT type, entity_id, origin_node FROM events ORDER BY type") == [
            ("agent.updated", "a-edge", EDGE), ("task.created", "t1", EDGE)]
        _sql(path, "UPDATE agents SET current_task = 'changed here since'")
        with engine.begin() as conn:  # the same answer again (a replayed receipt) settles nothing
            ledger.settle(conn, ops[0].op_id, verdict)
        assert _sql(path, "SELECT current_task FROM agents") == [("changed here since",)]
        with engine.begin() as conn:  # the staged one: its row update was made when it was staged
            ledger.settle(conn, ops[1].op_id, verdict.model_copy(update={"op_id": ops[1].op_id}))
        assert _sql(path, "SELECT current_task FROM agents") == [("changed here since",)]
    finally:
        engine.dispose()


def _op(path: str, body: dict | None = None) -> SyncOperation:
    clock = hlc.encode(hlc.Hlc(hlc.now_ms(), 0, EDGE))
    return SyncOperation(op_id=time_ordered_id(), origin_node=EDGE, hlc=clock, method="POST", path=path,
                         body=json.dumps(body or {}), headers={"content-type": "application/json"},
                         sync_protocol=SYNC_PROTOCOL)


def _center_app(repository: StorageRepository) -> FastAPI:
    app = FastAPI()

    @app.post("/via-the-rules")
    @route_surface(RouteSurface.shared(offline_ok=True))
    async def via_the_rules() -> dict:
        await repository.create_project(name="kept", root_path="/kept")
        await repository.update_agent("a-edge", current_task="handed back")
        return {"success": True}

    @app.post("/deletes-a-session-row")
    @route_surface(RouteSurface.shared(offline_ok=True))
    async def deletes_a_session_row() -> dict:
        async with get_session(repository.db_url) as session:
            await session.run_sync(lambda s: s.connection().exec_driver_sql(
                "DELETE FROM events WHERE id = 'center-own-event'"))
        return {"success": True}

    @app.post("/around-the-rules")
    @route_surface(RouteSurface.shared(offline_ok=True))
    async def around_the_rules() -> dict:
        await repository.create_project(name="rolled back", root_path="/rolled-back")
        async with get_session(repository.db_url) as session:
            await session.run_sync(lambda s: s.connection().exec_driver_sql(
                "UPDATE agents SET current_task = 'written on the center' WHERE id = 'a-edge'"))
        return {"success": True}

    return app


async def test_a_session_write_of_another_node_fails_the_operation_and_leaves_nothing(node) -> None:
    repository, path = node
    _as_center(path)
    _agent(path, "a-edge", EDGE)
    operations = Operations(_center_app(repository), repository, CENTER, Intake(repository.db_url, CENTER))
    applied = await operations.apply(_op("/via-the-rules"))
    assert applied.outcome is OpOutcome.APPLIED
    assert [e.pk for e in applied.effects if e.kind is SyncEffectKind.ROW_UPDATE] == ["a-edge"]
    refused = _op("/around-the-rules")
    with pytest.raises(TemporaryFailureError, match="单写者"):
        await operations.apply(refused)
    assert _sql(path, "SELECT current_task FROM agents") == [(None,)]
    assert _sql(path, "SELECT name FROM projects") == [("kept",)]
    assert _sql(path, "SELECT count(*) FROM sync_op_receipts WHERE op_id = ?", refused.op_id) == [(0,)]
    assert _sql(path, "SELECT count(*) FROM sync_guard") == [(0,)]
    assert _sql(path, "SELECT count(*) FROM sync_changes WHERE table_name IN ('agents', 'events')") == [(0,)]
    _sql(path, "INSERT INTO events (id, type, source, data, timestamp, origin_node) VALUES "
               "('center-own-event', 'task.created', 'test', '{}', '2026-11-15 08:00:00', ?)", CENTER)
    with pytest.raises(TemporaryFailureError, match="删除"):  # even the center's own row: never for another node
        await operations.apply(_op("/deletes-a-session-row"))
    assert _sql(path, "SELECT count(*) FROM events WHERE id = 'center-own-event'") == [(1,)]


def test_an_effect_on_a_table_that_is_not_the_session_face_is_ignored(node) -> None:
    _, path = node
    engine = create_engine(f"sqlite:///{path}")
    try:
        with engine.begin() as conn:
            effects.apply(conn, [effects.changed_fields("agents", "nope", {"current_task": "x"}).model_copy(
                update={"table_name": "projects"})], staged=False)
    finally:
        engine.dispose()
    assert _sql(path, "SELECT count(*) FROM projects") == [(0,)]


async def test_an_event_handed_back_is_stored_as_this_node_would_store_it(node) -> None:
    """The center builds the handed-back event from the ORM row; written on the edge it
    must read the same as one the edge wrote itself (a JSON None is the text ``null``)."""
    repository, path = node
    await repository.create_event("task.created", "test", {"n": 1}, entity_id="here")
    sync_context.set_process_node(CENTER, "center", own_origins=(CENTER,))
    with sync_context.operation(time_ordered_id(), origin=EDGE, staged=False) as carried:
        await repository.create_event("task.created", "test", {"n": 1}, entity_id="handed-back")
    assert [effect.kind for effect in carried.effects] == [SyncEffectKind.EVENT]
    engine = create_engine(f"sqlite:///{path}")
    try:
        with engine.begin() as conn:
            effects.apply(conn, carried.effects, staged=False)
    finally:
        engine.dispose()
    columns = "typeof(state_snapshot), state_snapshot, typeof(data), data, typeof(timestamp)"
    here = _sql(path, f"SELECT {columns} FROM events WHERE entity_id = 'here'")
    handed_back = _sql(path, f"SELECT {columns} FROM events WHERE entity_id = 'handed-back'")
    assert here == handed_back == [("text", "null", "text", '{"n": 1}', "text")]
