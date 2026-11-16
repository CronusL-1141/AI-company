"""One transaction for a whole operation (aiteam.storage.connection.unit_of_work).

Every repository call inside it shares one session through savepoints: a failed call
undoes only itself, as separate sessions did; an error leaving the block undoes
everything; the commit happens once, at the end. Read-modify-writes that take the
write lock themselves (BEGIN IMMEDIATE) run inside it unchanged. Read back through
fresh sqlite3 connections.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import text

from aiteam.storage.connection import close_db, unit_of_work
from aiteam.storage.repository import StorageRepository
from aiteam.sync import context as sync_context
from aiteam.types import SyncEffectKind


@pytest.fixture()
async def repo(tmp_path: Path):
    repository = StorageRepository(db_url=f"sqlite+aiosqlite:///{tmp_path / 'uow.db'}")
    await repository.init_db()
    yield repository, tmp_path / "uow.db"
    await close_db()


def _rows(path: Path, query: str) -> list[tuple]:
    con = sqlite3.connect(path)
    try:
        return con.execute(query).fetchall()
    finally:
        con.close()


async def _mark(session) -> None:
    await session.execute(text("UPDATE sync_state SET hlc = hlc WHERE id = 1"))


async def test_calls_share_one_transaction_committed_at_the_end(repo) -> None:
    repository, path = repo
    project = await repository.create_project(name="p", root_path="/p")
    async with unit_of_work(_mark, repository.db_url):
        task = await repository.create_managed_task(project.id, None, {"title": "inside"})
        await repository.update_managed_task(task.id, {"status": "running"})  # takes the lock itself
        assert _rows(path, "SELECT count(*) FROM tasks") == [(0,)]  # not committed yet
    assert _rows(path, "SELECT title, status FROM tasks") == [("inside", "running")]


async def test_a_failed_call_undoes_itself_only(repo) -> None:
    repository, path = repo
    project = await repository.create_project(name="p", root_path="/p")
    async with unit_of_work(_mark, repository.db_url):
        await repository.create_managed_task(project.id, None, {"title": "kept"})
        with pytest.raises(Exception, match="不存在"):
            await repository.update_managed_task("missing", {"status": "running"})
        await repository.create_managed_task(project.id, None, {"title": "also kept"})
    assert sorted(_rows(path, "SELECT title FROM tasks")) == [("also kept",), ("kept",)]


async def test_an_error_leaving_the_block_undoes_everything(repo) -> None:
    repository, path = repo
    project = await repository.create_project(name="p", root_path="/p")
    with pytest.raises(RuntimeError):
        async with unit_of_work(_mark, repository.db_url):
            await repository.create_managed_task(project.id, None, {"title": "gone"})
            raise RuntimeError("operation failed")
    assert _rows(path, "SELECT count(*) FROM tasks") == [(0,)]
    project_after = await repository.create_managed_task(project.id, None, {"title": "after"})
    assert project_after.title == "after"  # the database is usable again


async def test_no_event_of_another_nodes_operation_is_stored_here_and_one_goes_back_with_its_time(repo) -> None:
    """A staged write's events are the center's to emit; the center's, for an edge's
    operation, are the edge's to store: they go back in the verdict, dated by the operation."""
    repository, path = repo
    with sync_context.operation("0193a3e4-0000-7000-8000-000000000001", origin="air-3f9c", staged=True) as staged:
        await repository.create_event("task.created", "test", {"n": 1})
    wall = datetime(2026, 11, 15, 1, 2, 3, tzinfo=UTC)
    with sync_context.operation("0193a3e4-0000-7000-8000-000000000002", origin="air-3f9c", staged=False,
                                wall=wall) as applied:
        event = await repository.create_event("task.created", "test", {"n": 2})
    assert _rows(path, "SELECT id FROM events") == [] and staged.effects == []
    [effect] = applied.effects
    assert (effect.kind, effect.table_name, effect.pk) == (SyncEffectKind.EVENT, "events", event.id)
    assert effect.row["origin_node"] == "air-3f9c" and effect.row["data"] == '{"n": 2}'
    assert effect.row["timestamp"].startswith("2026-11-15 01:02:03")


async def test_rows_carry_the_origin_of_the_operation_they_were_written_for(repo) -> None:
    repository, path = repo
    await repository.create_project(name="plain", root_path="/plain")
    with sync_context.operation("0193a3e4-0000-7000-8000-000000000003", origin="air-3f9c", staged=False):
        await repository.create_project(name="for-air", root_path="/air")
    assert sorted(_rows(path, "SELECT name, origin_node FROM projects")) == [("for-air", "air-3f9c"), ("plain", "")]


async def test_the_earliest_invalidation_stands_and_an_offline_memo_keeps_its_time(repo) -> None:
    repository, path = repo
    memory = await repository.create_memory(scope="global", scope_id="system", content="rule")
    await repository.invalidate_memory(memory.id, invalidated_by="later-here")
    earlier = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    with sync_context.operation("0193a3e4-0000-7000-8000-000000000004", origin="air-3f9c", staged=False,
                                wall=earlier):
        await repository.invalidate_memory(memory.id, invalidated_by="earlier-elsewhere")
        project = await repository.create_project(name="p", root_path="/p")
        task = await repository.create_managed_task(project.id, None, {"title": "t"})
        memo = await repository.add_task_memo(task.id, content="written offline")
    await repository.invalidate_memory(memory.id, invalidated_by="later-again")
    assert _rows(path, "SELECT invalidated_by FROM memories") == [("earlier-elsewhere",)]
    assert _rows(path, f"SELECT created_at FROM task_memos WHERE id = '{memo.id}'")[0][0].startswith("2026-01-02")


async def test_the_center_does_not_look_for_another_machines_folder(repo, monkeypatch) -> None:
    from fastapi import HTTPException

    from aiteam.api.routes.projects import _checked_root_path

    repository, _ = repo
    with pytest.raises(HTTPException, match="不存在"):
        await _checked_root_path("/nowhere/on/this/machine", "p1", repository)
    sync_context.set_process_node("mini-c0de", "center")
    try:
        with sync_context.operation("0193a3e4-0000-7000-8000-000000000005", origin="air-3f9c", staged=False):
            assert await _checked_root_path("/nowhere/on/this/machine/", "p1", repository) == "/nowhere/on/this/machine"
        with pytest.raises(HTTPException), sync_context.operation(
                "0193a3e4-0000-7000-8000-000000000006", origin="mini-c0de", staged=False):
            await _checked_root_path("/nowhere/on/this/machine", "p1", repository)
    finally:
        sync_context.set_process_node("", "standalone")
