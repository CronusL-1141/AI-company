"""The center takes in an edge's session face: one writer per origin, in order, never older rows.

Batches are applied through ``Intake`` on a center's database; what the center
holds is read back through fresh connections.
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from pathlib import Path

import pytest

from aiteam.storage.connection import close_db
from aiteam.storage.repository import StorageRepository
from aiteam.sync import context as sync_context
from aiteam.sync import intake as intake_module
from aiteam.sync.intake import Intake
from aiteam.types import SyncChange, SyncIntakeOutcome, SyncSessionBatch

CENTER, EDGE, OTHER = "mini-c0de", "air-ed9e", "pro-0b0e"


@pytest.fixture()
async def center(tmp_path: Path):
    path = tmp_path / "center.db"
    repository = StorageRepository(db_url=f"sqlite+aiosqlite:///{path}")
    await repository.init_db()
    _sql(path, f"UPDATE sync_state SET capture = 1, node_id = '{CENTER}'")
    yield Intake(repository.db_url, CENTER), path
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


def _event(event_id: str, origin: str, source: str = "test") -> dict:
    return {"id": event_id, "type": "task.created", "source": source, "data": "{}",
            "timestamp": "2026-11-15 08:00:00.000000", "entity_id": None, "entity_type": None,
            "state_snapshot": None, "origin_node": origin}


def _team(team_id: str, name: str, origin: str) -> dict:
    return {"id": team_id, "name": name, "mode": "coordinate", "status": "active", "summary": "",
            "config": "{}", "created_at": "2026-11-15 08:00:00.000000", "updated_at": "2026-11-15 08:00:00.000000",
            "origin_node": origin}


def _batch(origin: str, *changes: tuple[str, str, dict | None], after: int = 0, upto: int | None = None,
           snapshot: int | None = None) -> SyncSessionBatch:
    end = upto if upto is not None else after + len(changes)
    return SyncSessionBatch(origin_node=origin, after=after, upto=end,
                            snapshot=snapshot if snapshot is not None else end,
                            changes=[SyncChange(seq=after + n + 1, table_name=t, pk=pk, row=row)
                                     for n, (t, pk, row) in enumerate(changes)])


def _insert(path: Path, table: str, row: dict) -> None:
    _sql(path, f"INSERT INTO {table} ({', '.join(row)}) VALUES ({', '.join('?' for _ in row)})", *row.values())


async def test_rows_of_the_batch_origin_are_taken_and_the_cursor_moves(center) -> None:
    intake, path = center
    ack = await intake.apply(_batch(EDGE, ("events", "e1", _event("e1", EDGE)), ("events", "e2", _event("e2", EDGE))))
    assert (ack.outcome, ack.cursor, ack.applied) == (SyncIntakeOutcome.APPLIED, 2, 2)
    assert _sql(path, "SELECT id, origin_node FROM events ORDER BY id") == [("e1", EDGE), ("e2", EDGE)]
    assert await intake.cursor(EDGE) == 2
    gone = await intake.apply(_batch(EDGE, ("events", "e1", None), after=2))
    assert gone.applied == 1 and _sql(path, "SELECT id FROM events") == [("e2",)]


async def test_the_first_edge_owns_the_baseline_and_no_other_node_writes_it(center) -> None:
    intake, path = center
    _insert(path, "events", _event("base", ""))
    first = await intake.apply(_batch(EDGE, ("events", "base", _event("base", "", source="edited by the edge"))))
    assert first.applied == 1 and await intake.baseline_owner() == EDGE
    second = await intake.apply(_batch(OTHER, ("events", "base", _event("base", "", source="another edge"))))
    claims = await intake.apply(_batch(OTHER, ("events", "x", _event("x", EDGE)), after=1))
    assert (second.skipped_foreign, claims.refused) == (1, 1)
    assert _sql(path, "SELECT source FROM events WHERE id = 'base'") == [("edited by the edge",)]
    assert _sql(path, "SELECT count(*) FROM events WHERE id = 'x'") == [(0,)]
    assert [(s.node, s.baseline_owner, s.refused) for s in await intake.statuses()] == [
        (EDGE, True, 0), (OTHER, False, 1)]


async def test_a_row_the_center_owns_is_never_overwritten_or_deleted_by_an_edge(center) -> None:
    intake, path = center
    _insert(path, "events", _event("mine", CENTER, source="the center's"))
    ack = await intake.apply(_batch(EDGE, ("events", "mine", _event("mine", EDGE)), ("events", "mine", None)))
    assert (ack.applied, ack.skipped_foreign) == (0, 2)
    assert _sql(path, "SELECT source, origin_node FROM events") == [("the center's", CENTER)]


async def test_a_gap_or_a_stale_batch_changes_nothing(center) -> None:
    intake, path = center
    await intake.apply(_batch(EDGE, ("events", "e1", _event("e1", EDGE)), upto=10, snapshot=12))
    gap = await intake.apply(_batch(EDGE, ("events", "e2", _event("e2", EDGE)), after=11))
    stale = await intake.apply(_batch(EDGE, ("events", "e1", _event("e1", EDGE, source="older")), after=5,
                                      upto=11, snapshot=11))
    assert (gap.outcome, gap.cursor) == (SyncIntakeOutcome.GAP, 10)
    assert (stale.outcome, stale.cursor) == (SyncIntakeOutcome.STALE, 10)
    assert _sql(path, "SELECT id, source FROM events") == [("e1", "test")]
    overlap = await intake.apply(_batch(EDGE, ("events", "e1", _event("e1", EDGE, source="newer")), after=5,
                                        upto=14, snapshot=14))
    assert (overlap.outcome, overlap.cursor) == (SyncIntakeOutcome.APPLIED, 14)
    assert _sql(path, "SELECT source FROM events") == [("newer",)]


async def test_a_unique_value_moves_within_one_owner_and_is_never_taken_from_another(center) -> None:
    intake, path = center
    _insert(path, "teams", _team("t-center", "session-shared", CENTER))
    _insert(path, "teams", _team("t-old", "session-edge", EDGE))
    taken = await intake.apply(_batch(EDGE, ("teams", "t-new", _team("t-new", "session-edge", EDGE)),
                                      ("teams", "t-steal", _team("t-steal", "session-shared", EDGE))))
    assert (taken.applied, taken.skipped_foreign) == (1, 1)
    assert sorted(_sql(path, "SELECT id, name FROM teams")) == [("t-center", "session-shared"),
                                                                ("t-new", "session-edge")]


async def test_a_fact_numbered_per_database_gets_the_centers_next_number(center) -> None:
    intake, path = center
    fact = {"observation_id": "o1", "source_namespace": "ns", "source_id": "src",
            "saved_at": "2026-11-15 08:00:00.000000", "payload": "{}", "seq": 7, "kind": "ledger",
            "occurred_at": None, "response_id": None, "session_id": None, "origin_node": EDGE, "origin_seq": None}
    _insert(path, "codex_usage_observations", {**fact, "observation_id": "own", "seq": 3, "origin_node": CENTER})
    await intake.apply(_batch(EDGE, ("codex_usage_observations", "o1", fact)))
    await intake.apply(_batch(EDGE, ("codex_usage_observations", "o1", {**fact, "kind": "count"}), after=1))
    assert _sql(path, "SELECT observation_id, seq, origin_seq, kind FROM codex_usage_observations ORDER BY seq") == [
        ("own", 3, None, "ledger"), ("o1", 4, 7, "count")]


async def test_taking_a_batch_in_holds_the_lock_at_most_a_quarter_of_the_time(center, monkeypatch) -> None:
    intake, _ = center
    slept: list[float] = []
    real_sleep = asyncio.sleep

    async def record(seconds: float) -> None:
        slept.append(seconds)
        await real_sleep(0)

    def slow_take_in(conn, batch):
        time.sleep(0.05)
        return intake_module.take_in(conn, batch)

    monkeypatch.setattr(intake_module.asyncio, "sleep", record)
    monkeypatch.setattr(intake_module, "_take_in_suppressed", slow_take_in)
    await intake.apply(_batch(EDGE, ("events", "e1", _event("e1", EDGE))))
    assert slept and slept[0] >= 0.05 * 3
    await intake.apply(_batch(EDGE, ("events", "e2", _event("e2", EDGE)), after=1), pace=False)
    assert len(slept) == 1  # a forward's tail is not held back


async def test_facts_numbered_alike_on_both_nodes_are_all_taken(center) -> None:
    """The edge's numbers mean nothing among the center's: an edge fact numbered like one
    of the center's own is taken under the center's next number, not skipped as its
    holder's (the center's own facts and the edge's interleave on both machines)."""
    intake, path = center

    def fact(observation: str, seq: int, origin: str) -> dict:
        return {"observation_id": observation, "source_namespace": "ns", "source_id": "src",
                "saved_at": "2026-11-15 08:00:00.000000", "payload": "{}", "seq": seq, "kind": "ledger",
                "occurred_at": None, "response_id": None, "session_id": None, "origin_node": origin,
                "origin_seq": None}

    for n in (1, 2, 3):
        _insert(path, "codex_usage_observations", fact(f"own-{n}", n, CENTER))
    ack = await intake.apply(_batch(EDGE, *[("codex_usage_observations", f"air-{n}", fact(f"air-{n}", n, EDGE))
                                            for n in (1, 2, 3, 4, 5)]))
    assert (ack.applied, ack.skipped_foreign) == (5, 0)
    edge_rows = _sql(path, "SELECT observation_id, seq, origin_seq FROM codex_usage_observations "
                           "WHERE origin_node = ? ORDER BY origin_seq", EDGE)
    assert [(o, k) for o, _, k in edge_rows] == [(f"air-{n}", n) for n in (1, 2, 3, 4, 5)]
    assert sorted(seq for _, seq, _ in edge_rows) == [4, 5, 6, 7, 8]
    assert _sql(path, "SELECT count(*) FROM codex_usage_observations WHERE origin_node = ?", CENTER) == [(3,)]


async def test_an_empty_value_under_a_partial_unique_index_holds_nothing(center) -> None:
    intake, path = center
    agent = {"team_id": "t", "role": "worker", "system_prompt": "", "model": "", "status": "waiting",
             "config": "{}", "source": "api", "trust_score": 0.5, "created_at": "2026-11-15 08:00:00",
             "cc_tool_use_id": "", "origin_node": EDGE}
    _insert(path, "agents", {"id": "a-old", "name": "old", **agent})
    ack = await intake.apply(_batch(EDGE, ("agents", "a-new", {"id": "a-new", "name": "new", **agent})))
    assert ack.applied == 1
    assert sorted(_sql(path, "SELECT id FROM agents")) == [("a-new",), ("a-old",)]


async def test_the_keys_held_for_an_edge_are_its_own_and_the_baseline_only_for_its_owner(center) -> None:
    """``resend-session`` deletes what the center holds for the edge and the edge does not
    have: the listing must never offer a row the edge does not own."""
    intake, path = center
    await intake.apply(_batch(EDGE, ("events", "e1", _event("e1", EDGE))))  # EDGE: the first, owns the baseline
    for event_id, origin in (("e2", EDGE), ("b1", ""), ("c1", CENTER), ("o1", OTHER)):
        _insert(path, "events", _event(event_id, origin))

    async def every_key(node: str, limit: int) -> list[str]:
        keys, after = [], 0
        while True:
            page = await intake.held_keys(node, "events", after, limit)
            keys += page.keys
            if page.done:
                return keys
            after = page.after

    assert sorted(await every_key(EDGE, 1)) == ["b1", "e1", "e2"]
    assert await every_key(OTHER, 100) == ["o1"]
    with pytest.raises(ValueError):
        await intake.held_keys(EDGE, "tasks", 0, 10)  # shared face: not an edge's
