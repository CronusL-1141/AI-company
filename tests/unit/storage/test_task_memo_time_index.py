"""The task-wall digest reads memos through ``idx_memos_task_created`` (task_id, created_at).

Its two memo reads (the open tasks' recent window, and the latest few before it for
each open task) must seek by time inside each task: with task_id alone every read
walks a task's whole history, and one task already had 461 memos on 2026-09-29.

Pinned like the compact-checkpoint index: the model declares it for new databases,
the startup migration adds it to old ones, and the plan of the SQL production
actually sends (captured from the engine, parameters included) uses it.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event

from aiteam.clock import utc_now
from aiteam.storage.connection import _sqlite_migrate, close_db, get_engine
from aiteam.storage.models import Base, TaskMemoModel
from aiteam.storage.repository import StorageRepository

INDEX = "idx_memos_task_created"


def _index_sql(db_path: Path) -> str | None:
    with sqlite3.connect(db_path) as con:
        row = con.execute("SELECT sql FROM sqlite_master WHERE type='index' AND name=?", (INDEX,)).fetchone()
    return row[0] if row else None


def test_new_database_gets_the_index_from_the_model(tmp_path: Path):
    db_path = tmp_path / "model-only.sqlite"
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        Base.metadata.create_all(engine, tables=[TaskMemoModel.__table__])
    finally:
        engine.dispose()
    assert "task_id, created_at" in (_index_sql(db_path) or "")


@pytest.mark.asyncio
async def test_existing_database_gets_the_index_from_the_startup_migration(tmp_path: Path):
    db_path = tmp_path / "memos.sqlite"
    await StorageRepository(db_url=f"sqlite+aiosqlite:///{db_path}").init_db()
    await close_db()
    with sqlite3.connect(db_path) as con:  # an old database: the table without the index
        con.execute(f"DROP INDEX {INDEX}")
    assert _index_sql(db_path) is None
    _sqlite_migrate(str(db_path))
    sql = _index_sql(db_path)
    assert sql is not None
    _sqlite_migrate(str(db_path))  # idempotent
    assert _index_sql(db_path) == sql


@pytest.mark.asyncio
async def test_the_digest_memo_reads_seek_by_time(tmp_path: Path):
    db_path = tmp_path / "plan.sqlite"
    url = f"sqlite+aiosqlite:///{db_path}"
    repo = StorageRepository(db_url=url)
    await repo.init_db()
    project = await repo.create_project(name="plan", root_path="/tmp/plan")
    task = await repo.create_task(team_id=None, title="open", project_id=project.id)
    await repo.add_task_memo(task.id, "note", project_id=project.id)
    done = await repo.create_task(team_id=None, title="done", project_id=project.id)
    await repo.add_task_memo(done.id, "note", project_id=project.id)
    await repo.update_task(done.id, status="completed")
    captured: list[tuple[str, tuple]] = []

    def _capture(_conn, _cursor, statement, parameters, _context, _executemany):
        if "task_memos" in statement:
            captured.append((statement, tuple(parameters)))

    engine = get_engine(url).sync_engine
    event.listen(engine, "before_cursor_execute", _capture)
    try:
        now = utc_now()
        records = await repo.list_task_activity(
            project.id, since=now - timedelta(days=35), closed_since=now - timedelta(days=7))
    finally:
        event.remove(engine, "before_cursor_execute", _capture)
        await close_db()

    assert {r.task_id for r in records if r.source == "memo"} == {task.id}, "no memo of a completed task"
    assert len(captured) == 2, [statement for statement, _ in captured]  # window, older
    with sqlite3.connect(db_path) as con:
        for statement, parameters in captured:
            plan = " | ".join(row[-1] for row in con.execute(f"EXPLAIN QUERY PLAN {statement}", parameters))
            assert INDEX in plan, plan
            assert not re.search(r"SCAN (task_memos|m|o)\b", plan), plan
