"""What an edge sends of its session face, and what it keeps of it.

Session rows are written with change capture on (an edge captures both faces); the
batches are read back as the worker and a forward read them, on the synchronous and
on the async engine.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import create_engine

from aiteam.storage.connection import close_db, get_engine
from aiteam.storage.repository import StorageRepository
from aiteam.sync import hlc, ledger, upload
from aiteam.types import SyncSessionBatch

EDGE = "air-ed9e"


@pytest.fixture()
async def edge(tmp_path: Path):
    path = tmp_path / "edge.db"
    repository = StorageRepository(db_url=f"sqlite+aiosqlite:///{path}")
    await repository.init_db()
    _sql(path, f"UPDATE sync_state SET capture = 3, node_id = '{EDGE}'")
    engine = create_engine(f"sqlite:///{path}")
    with engine.begin() as conn:
        ledger.ensure_peer(conn)
    yield engine, path, repository
    engine.dispose()
    await close_db()


def _sql(path: Path, query: str, *params) -> list[tuple]:
    con = sqlite3.connect(path)
    try:
        rows = con.execute(query, params).fetchall()
        con.commit()
        return rows
    finally:
        con.close()


def _event(path: Path, event_id: str, source: str = "test") -> None:
    _sql(path, "INSERT INTO events (id, type, source, data, timestamp, origin_node) VALUES "
               "(?, 'task.created', ?, '{}', '2026-11-15 08:00:00', ?)", event_id, source, EDGE)


def _read(engine, after: int = 0, **limits) -> SyncSessionBatch:
    with engine.connect() as conn:
        return upload.read_batch(conn, EDGE, after, **limits)


def test_a_batch_holds_each_row_once_as_it_is_now_and_cuts_at_the_operation(edge) -> None:
    engine, path, _ = edge
    _event(path, "e1")
    _event(path, "e2")
    _sql(path, "UPDATE events SET source = 'later' WHERE id = 'e1'")
    _sql(path, "DELETE FROM events WHERE id = 'e2'")
    whole = _read(engine)
    assert [(c.pk, (c.row or {}).get("source")) for c in whole.changes] == [("e1", "later"), ("e2", None)]
    assert (whole.after, whole.upto, whole.snapshot) == (0, 4, 4)
    first = _read(engine, upto=1)
    assert [c.pk for c in first.changes] == ["e1"] and (first.upto, first.snapshot) == (1, 4)
    assert first.changes[0].row["source"] == "later"  # read as it is now: harmless, never older


def test_a_batch_is_cut_short_by_rows_or_bytes_and_says_how_far_it_got(edge) -> None:
    engine, path, _ = edge
    for n in range(5):
        _event(path, f"e{n}", source="x" * 400)
    assert _read(engine, max_rows=2).upto == 2
    small = _read(engine, max_bytes=1500)
    assert [c.pk for c in small.changes] == ["e0", "e1"] and small.upto == 2
    alone = _read(engine, max_bytes=10)
    assert [c.pk for c in alone.changes] == ["e0"] and alone.upto == 1  # one large row goes alone


def test_shared_changes_are_not_session_rows_but_the_position_passes_them(edge) -> None:
    engine, path, _ = edge
    _sql(path, "INSERT INTO phases (id, project_id, name, description, status, \"order\", config, created_at, "
               "updated_at) VALUES ('ph', 'p', 'n', '', 'planning', 0, '{}', '2026-01-01', '2026-01-01')")
    batch = _read(engine)
    assert batch.changes == [] and batch.upto == 1  # an operation waiting for seq 1 is not held up


async def test_a_forward_reads_its_tail_on_the_async_engine_too(edge) -> None:
    _, path, repository = edge
    _event(path, "e1")
    async with get_engine(repository.db_url).connect() as conn:
        batch = await conn.run_sync(lambda c: upload.read_batch(c, EDGE, 0, upto=1))
    assert [c.pk for c in batch.changes] == ["e1"]


def test_the_center_is_the_authority_on_where_this_edge_stands_within_two_bounds(edge) -> None:
    """The edge stands where the center says, but never before what it pruned (the center
    lost rows only a resend restores) nor past its own sequence (this database went back:
    its later changes must not be skipped)."""
    engine, path, _ = edge
    for n in range(12):
        _event(path, f"e{n}")
    with engine.begin() as conn:
        upload.take_ack(conn, 9)
    assert _sql(path, "SELECT up_cursor FROM sync_peers") == [(9,)]
    with engine.begin() as conn:
        upload.take_ack(conn, 4)  # the center lost 5..9: sent again from 4
    assert _sql(path, "SELECT up_cursor FROM sync_peers") == [(4,)]
    _sql(path, "UPDATE sync_peers SET up_pruned = 6")
    with pytest.raises(upload.PositionError, match="resend-session"), engine.begin() as conn:
        upload.take_ack(conn, 4)
    with pytest.raises(upload.PositionError, match="本机库可能被旧备份恢复"), engine.begin() as conn:
        upload.take_ack(conn, 13)
    assert _sql(path, "SELECT up_cursor FROM sync_peers") == [(4,)]


def test_the_handshake_rewinds_to_a_center_that_lost_the_end_but_not_before_the_pruned(edge) -> None:
    engine, path, _ = edge
    for n in range(10):
        _event(path, f"e{n}")
    _sql(path, "UPDATE sync_peers SET up_cursor = 9, up_pruned = 3")
    with engine.begin() as conn:
        assert upload.rewind(conn, 5)
    assert _sql(path, "SELECT up_cursor FROM sync_peers") == [(5,)]
    with engine.begin() as conn:
        assert not upload.rewind(conn, 2)  # before the pruned: only a resend helps
        assert not upload.rewind(conn, 7)  # ahead: nothing to send again
    assert _sql(path, "SELECT up_cursor FROM sync_peers") == [(5,)]


def test_pruning_keeps_what_the_center_lacks_and_what_is_recent(edge) -> None:
    engine, path, _ = edge
    for n in range(4):
        _event(path, f"e{n}")
    _sql(path, "UPDATE sync_peers SET up_cursor = 3")
    old = hlc.encode(hlc.Hlc(1, 0, EDGE))
    _sql(path, "UPDATE sync_changes SET hlc = ? WHERE seq <= 2", old)
    with engine.begin() as conn:
        assert upload.prune(conn) == 2
    assert [seq for (seq,) in _sql(path, "SELECT seq FROM sync_changes ORDER BY seq")] == [3, 4]
    assert _sql(path, "SELECT up_pruned FROM sync_peers") == [(2,)]
    with engine.begin() as conn:
        assert upload.prune(conn) == 0  # seq 3 is recent, seq 4 not taken in yet
    _event(path, "after-prune")
    assert _sql(path, "SELECT max(seq) FROM sync_changes") == [(5,)]  # the sequence never goes back


def test_a_full_resend_holds_every_row_of_this_node_and_the_baseline_and_resets_the_position(edge) -> None:
    engine, path, _ = edge
    _event(path, "mine")
    for event_id, origin in (("baseline", ""), ("elsewhere", "pro-0b0e")):
        _sql(path, "INSERT INTO events (id, type, source, data, timestamp, origin_node) VALUES "
                   "(?, 'task.created', 'test', '{}', '2026-11-15 08:00:00', ?)", event_id, origin)
    with engine.connect() as conn:
        pages = list(upload.resend_pages(conn, EDGE, page=1))
    assert sorted(change.pk for _, changes in pages for change in changes) == ["baseline", "mine"]
    assert all(len(changes) == 1 for _, changes in pages)
    _sql(path, "UPDATE sync_peers SET up_cursor = 90, up_pruned = 80")
    with engine.begin() as conn:
        upload.resent(conn, 12)
    assert _sql(path, "SELECT up_cursor, up_pruned FROM sync_peers") == [(12, 12)]
