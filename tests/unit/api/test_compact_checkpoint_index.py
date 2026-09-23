"""压缩检查点读取必须走部分索引 ``ix_events_compact_checkpoint``。

compact 之后 session_bootstrap 取检查点（``GET /api/hooks/compact-checkpoint``），查询是
``type=? AND source=? ORDER BY timestamp DESC LIMIT 1``。只有 source 单列索引时，SQLite
要把该会话的全部事件（大会话实测 21.5 万行）捞出来再临时排序，冷页缓存下 1.0-1.3s，
正落在 bootstrap 的 2s 超时窗里（检查点注入因此被静默丢过）。

钉三件事：老库迁移补得上、新库建表带得上、**生产实际发出的那条 SQL**（含绑定参数）
的查询计划真的用它且不再临时排序。计划断言用的是从引擎上截下来的原句原参，不是
手抄的 SQL —— 手抄的 SQL 与 ORM 生成的差一个限定名就可能换计划。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event

from aiteam.api import compact_checkpoint
from aiteam.api.routes import hooks
from aiteam.storage.connection import _sqlite_migrate, close_db, get_engine
from aiteam.storage.models import Base, EventModel
from aiteam.storage.repository import StorageRepository

INDEX = "ix_events_compact_checkpoint"


def _index_sql(db_path: Path) -> str | None:
    with sqlite3.connect(db_path) as con:
        row = con.execute("SELECT sql FROM sqlite_master WHERE type='index' AND name=?", (INDEX,)).fetchone()
    return row[0] if row else None


async def _fresh_db(tmp_path: Path) -> tuple[StorageRepository, Path, str]:
    db_path = tmp_path / "events.sqlite"
    url = f"sqlite+aiosqlite:///{db_path}"
    repo = StorageRepository(db_url=url)
    await repo.init_db()
    return repo, db_path, url


def test_new_database_gets_the_partial_index_from_the_model(tmp_path: Path):
    """只跑 create_all、不跑启动迁移：新库/内存库的索引来自模型声明本身。"""
    db_path = tmp_path / "model-only.sqlite"
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        Base.metadata.create_all(engine, tables=[EventModel.__table__])
    finally:
        engine.dispose()
    sql = _index_sql(db_path)
    assert sql is not None
    assert "WHERE" in sql.upper() and compact_checkpoint.CHECKPOINT_EVENT in sql


@pytest.mark.asyncio
async def test_existing_database_gets_the_index_from_the_startup_migration(tmp_path: Path):
    _repo, db_path, _url = await _fresh_db(tmp_path)
    await close_db()
    with sqlite3.connect(db_path) as con:  # 模拟升级前的老库：表在、索引不在
        con.execute(f"DROP INDEX {INDEX}")
    assert _index_sql(db_path) is None
    _sqlite_migrate(str(db_path))
    sql = _index_sql(db_path)
    assert sql is not None and compact_checkpoint.CHECKPOINT_EVENT in sql
    _sqlite_migrate(str(db_path))  # 幂等：重复启动不报错、不重建
    assert _index_sql(db_path) == sql


@pytest.mark.asyncio
async def test_the_checkpoint_read_query_uses_the_partial_index(tmp_path: Path):
    repo, db_path, url = await _fresh_db(tmp_path)
    captured: list[tuple[str, tuple]] = []

    def _capture(_conn, _cursor, statement, parameters, _context, _executemany):
        if "FROM events" in statement:
            captured.append((statement, tuple(parameters)))

    engine = get_engine(url).sync_engine
    event.listen(engine, "before_cursor_execute", _capture)
    try:
        await repo.create_event(
            compact_checkpoint.CHECKPOINT_EVENT, "session:sid-idx", {"trigger": "manual"}
        )
        result = await hooks.read_compact_checkpoint(session_id="sid-idx", repo=repo)
        assert result["found"] is True
    finally:
        event.remove(engine, "before_cursor_execute", _capture)
        await close_db()

    reads = [(sql, params) for sql, params in captured if sql.lstrip().upper().startswith("SELECT")]
    assert len(reads) == 1, reads
    sql, params = reads[0]
    assert compact_checkpoint.CHECKPOINT_EVENT in params  # 绑定参数，不是字面量
    with sqlite3.connect(db_path) as con:
        plan = " | ".join(row[-1] for row in con.execute(f"EXPLAIN QUERY PLAN {sql}", params))
    assert INDEX in plan, plan
    assert "TEMP B-TREE" not in plan.upper(), plan
