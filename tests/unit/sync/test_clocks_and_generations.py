"""Field clocks, the clock floor, and generations of natural-key ids.

* A node that syncs keeps a clock per written field of the mergeable shared tables
  (sync_f_<t>); a standalone node keeps none.
* Clock values issued outside the change log (an operation's) and observed from a
  peer raise a floor the triggers tick from, so removing log rows never turns the
  clock back.
* A project or team re-created after its deletion takes the next generation of its
  natural-key id instead of the deleted one; two nodes knowing the same deletions agree.
Read back through fresh sqlite3 connections.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import create_engine

from aiteam import ids
from aiteam.storage.connection import close_db
from aiteam.storage.repository import StorageRepository
from aiteam.sync import changelog, hlc


@pytest.fixture()
async def node(tmp_path: Path):
    path = tmp_path / "node.db"
    repo = StorageRepository(db_url=f"sqlite+aiosqlite:///{path}")
    await repo.init_db()
    yield repo, path
    await close_db()
    ids.set_natural_ids(False)


def _sql(path: Path, *statements: str) -> list[tuple]:
    con = sqlite3.connect(path)
    try:
        rows = []
        for statement in statements:
            rows = con.execute(statement).fetchall()
        con.commit()
        return rows
    finally:
        con.close()


def _sync(path: Path, node_id: str = "mini-c0de") -> None:
    _sql(path, f"UPDATE sync_state SET capture = 1, node_id = '{node_id}'")
    ids.set_natural_ids(True)


async def test_a_syncing_node_clocks_each_field_it_writes_and_a_standalone_one_none(node) -> None:
    repo, path = node
    project = await repo.create_project(name="p", root_path="/p")
    task = await repo.create_managed_task(project.id, None, {"title": "t"})
    await repo.update_managed_task(task.id, {"title": "standalone edit"})
    assert _sql(path, "SELECT count(*) FROM sync_field_clock") == [(0,)]
    _sync(path)
    await repo.update_managed_task(task.id, {"title": "synced edit"})
    clocks = dict(_sql(path, f"SELECT field, node FROM sync_field_clock WHERE pk = '{task.id}'"))
    assert clocks["title"] == "mini-c0de" and "config" in clocks  # the revision lives in config
    assert "status" not in clocks and "origin_node" not in clocks  # unchanged / never clocked


def test_the_floor_carries_the_clock_past_issued_and_observed_values(tmp_path: Path) -> None:
    import asyncio

    path = tmp_path / "floor.db"
    repo = StorageRepository(db_url=f"sqlite+aiosqlite:///{path}")
    asyncio.run(repo.init_db())
    asyncio.run(close_db())
    _sql(path, "UPDATE sync_state SET capture = 1, node_id = 'air-3f9c'")
    engine = create_engine(f"sqlite:///{path}")
    try:
        with engine.begin() as conn:
            first, second = changelog.next_hlc(conn), changelog.next_hlc(conn)
        assert hlc.parse(second) > hlc.parse(first)  # issued values persist as the floor
        ahead = hlc.encode(hlc.Hlc(hlc.now_ms() + 60_000, 7, "mini-c0de"))
        with engine.begin() as conn:
            changelog.observe_hlc(conn, ahead)
            after = changelog.next_hlc(conn)
        assert hlc.parse(after)[:2] > hlc.parse(ahead)[:2]  # a later local event orders after the peer's
        far = hlc.encode(hlc.Hlc(hlc.now_ms() + 3_600_000, 0, "mini-c0de"))
        with pytest.raises(hlc.HlcDriftError), engine.begin() as conn:
            changelog.observe_hlc(conn, far)
        _sql(path, "INSERT INTO phases (id, project_id, name, description, status, \"order\", config, created_at, "
                   "updated_at) VALUES ('ph', 'p', 'n', '', 'planning', 0, '{}', '2026-01-01', '2026-01-01')")
        last = _sql(path, "SELECT hlc FROM sync_changes ORDER BY seq DESC LIMIT 1")[0][0]
        with engine.begin() as conn:
            changelog.raise_floor(conn)
            conn.exec_driver_sql("DELETE FROM sync_changes")
            settled = changelog.next_hlc(conn)
        assert hlc.parse(settled)[:2] > hlc.parse(last)[:2]  # removing the log did not turn it back
    finally:
        engine.dispose()


async def test_a_recreated_project_takes_the_next_generation_of_its_id(node) -> None:
    repo, path = node
    _sync(path)
    first = await repo.create_project(name="p", root_path="/work/p")
    assert first.id == ids.derived_id(ids.PROJECT_ROOT, "/work/p")
    await repo.delete_project(first.id)
    second = await repo.create_project(name="p again", root_path="/work/p/")
    assert second.id == ids.derived_id(ids.PROJECT_ROOT, "/work/p", generation=1) != first.id
    await repo.delete_project(second.id)
    third = await repo.create_project(name="p thrice", root_path="/work/p")
    assert third.id == ids.derived_id(ids.PROJECT_ROOT, "/work/p", generation=2)
    assert sorted(_sql(path, "SELECT table_name, pk FROM sync_tombstones")) == sorted(
        [("projects", first.id), ("projects", second.id)])


async def test_a_recreated_session_team_does_not_adopt_the_deleted_ones_rows(node) -> None:
    repo, path = node
    _sync(path, "air-3f9c")
    team = await repo.create_team(name="session-abc12345", mode="coordinate")
    await repo.delete_team(team.id)
    again = await repo.create_team(name="session-abc12345", mode="coordinate")
    assert again.id == ids.derived_id(ids.TEAM_NAME, "session-abc12345", generation=1) != team.id


async def test_a_standalone_node_remembers_no_deletion(node) -> None:
    repo, path = node
    project = await repo.create_project(name="p", root_path="/work/p")
    await repo.delete_project(project.id)
    assert _sql(path, "SELECT count(*) FROM sync_tombstones") == [(0,)]


async def test_fields_kept_in_config_get_clocks_of_their_own(node) -> None:
    repo, path = node
    project = await repo.create_project(name="p", root_path="/p")
    task = await repo.create_managed_task(project.id, None, {"title": "t"})
    _sync(path)
    await repo.update_managed_task(task.id, {"status_note": "noted"})
    clocks = {field for (field,) in _sql(path, f"SELECT field FROM sync_field_clock WHERE pk = '{task.id}'")}
    assert "status_note" in clocks and "waiting_on" not in clocks
    # A config that is not JSON must not fail the write it observes.
    _sql(path, f"UPDATE tasks SET config = 'not json' WHERE id = '{task.id}'",
         f"UPDATE tasks SET config = 'still not json', title = 'x' WHERE id = '{task.id}'")
    assert _sql(path, f"SELECT title FROM tasks WHERE id = '{task.id}'") == [("x",)]
