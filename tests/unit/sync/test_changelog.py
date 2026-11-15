"""Change capture: what the triggers record, when they stay silent, how they are reinstalled."""

from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import create_engine

from aiteam.storage.connection import UTC_CLOCK_MARKER, close_db, init_db
from aiteam.storage.repository import StorageRepository
from aiteam.sync import changelog, hlc, tables
from aiteam.sync.tables import SYNCED_TABLES

ROOT = Path(__file__).resolve().parents[3]


def _url(path: Path) -> str:
    return f"sqlite+aiosqlite:///{path}"


def _changes(path: Path) -> list[tuple[str, str, str, str]]:
    con = sqlite3.connect(path)
    try:
        return con.execute("SELECT table_name, pk, kind, hlc FROM sync_changes ORDER BY seq").fetchall()
    finally:
        con.close()


def _sql(path: Path, *statements: str) -> None:
    con = sqlite3.connect(path)
    try:
        for statement in statements:
            con.execute(statement)
        con.commit()
    finally:
        con.close()


def _sql_rows(path: Path, statement: str) -> list[tuple]:
    con = sqlite3.connect(path)
    try:
        return con.execute(statement).fetchall()
    finally:
        con.close()


def _problems(path: Path) -> list[str]:
    engine = create_engine(f"sqlite:///{path}")
    try:
        with engine.connect() as conn:
            return changelog.trigger_problems(conn)
    finally:
        engine.dispose()


@pytest.fixture()
async def database(tmp_path: Path):
    path = tmp_path / "node.db"
    repo = StorageRepository(db_url=_url(path))
    await repo.init_db()
    yield repo, path
    await close_db()


def _capture(path: Path, node: str = "air-3f9c") -> None:
    """Capture both faces (shared = 1, session = 2): agents carries live triggers too."""
    _sql(path, f"UPDATE sync_state SET capture = 3, node_id = '{node}'")


async def test_a_new_database_carries_every_trigger_and_records_nothing(database) -> None:
    repo, path = database
    assert _problems(path) == []
    con = sqlite3.connect(path)
    triggers = con.execute("SELECT count(*) FROM sqlite_master WHERE type = 'trigger'").fetchone()[0]
    state = con.execute("SELECT id, node_id, capture FROM sync_state").fetchall()
    con.close()
    extra = len(tables.FIELD_CLOCK_TABLES) + len(tables.TOMBSTONE_TABLES)
    assert triggers == 3 * len(SYNCED_TABLES) + extra and state == [(1, "", 0)]
    await repo.create_project(name="p", root_path="/x")
    assert _changes(path) == []  # standalone: triggers present, capture off


async def test_capture_records_insert_update_delete_in_clock_order(database) -> None:
    repo, path = database
    _capture(path)
    project = await repo.create_project(name="p", root_path="/x")
    await repo.update_project(project.id, name="q")
    await repo.delete_project(project.id)
    changes = [c for c in _changes(path) if c[0] == "projects"]
    assert [(t, pk, kind) for t, pk, kind, _ in changes] == [
        ("projects", project.id, "I"), ("projects", project.id, "U"), ("projects", project.id, "D")]
    clocks = [hlc.parse(c[3]) for c in _changes(path)]
    assert all(clock.node == "air-3f9c" for clock in clocks)
    assert clocks == sorted(clocks) and len(set(clocks)) == len(clocks)


async def test_changed_keys_local_columns_and_natural_keys(database) -> None:
    _, path = database
    _capture(path)
    _sql(path,
         "INSERT INTO memories (id, scope, scope_id, content, kind, metadata, source_refs, created_at, accessed_at) "
         "VALUES ('m1', 'global', 'system', 'c', 'preference', '{}', '[]', '2026-01-01', '2026-01-01')",
         "UPDATE memories SET accessed_at = '2026-01-02' WHERE id = 'm1'",  # a read trace: not a change
         "UPDATE memories SET content = 'd' WHERE id = 'm1'",
         "UPDATE memories SET id = 'm2' WHERE id = 'm1'",
         "INSERT INTO knowledge_links (from_kind, from_id, to_kind, to_id, link_type, link_source, context, "
         "project_id, created_at) VALUES ('task', 't1', 'report', 'r1', 'cites', 'auto', '{}', '', '2026-01-01')")
    assert [(t, pk, kind) for t, pk, kind, _ in _changes(path)] == [
        ("memories", "m1", "I"), ("memories", "m1", "U"),
        ("memories", "m1", "D"), ("memories", "m2", "U"),
        ("knowledge_links", '["task","t1","report","r1","cites"]', "I")]


async def test_applier_suppression_lasts_only_its_transaction(database) -> None:
    _, path = database
    _capture(path)
    engine = create_engine(f"sqlite:///{path}")
    try:
        with engine.begin() as conn, changelog.suppressed(conn):
            conn.exec_driver_sql("INSERT INTO phases (id, project_id, name, description, status, \"order\", config, "
                                 "created_at, updated_at) VALUES ('ph1', 'p', 'n', '', 'planning', 0, '{}', "
                                 "'2026-01-01', '2026-01-01')")
    finally:
        engine.dispose()
    _sql(path, "UPDATE phases SET name = 'm' WHERE id = 'ph1'")
    assert [(t, pk, kind) for t, pk, kind, _ in _changes(path)] == [("phases", "ph1", "U")]
    assert sqlite3.connect(path).execute("SELECT count(*) FROM sync_suppress").fetchone()[0] == 0


async def test_a_failed_transaction_takes_its_marker_with_it(database) -> None:
    _, path = database
    _capture(path)
    engine = create_engine(f"sqlite:///{path}")
    try:
        with pytest.raises(RuntimeError), engine.begin() as conn, changelog.suppressed(conn):
            conn.exec_driver_sql("UPDATE sync_state SET node_id = 'air-3f9c' WHERE id = 1")
            raise RuntimeError("applier failed")
    finally:
        engine.dispose()
    _sql(path, "INSERT INTO task_memos (id, task_id, author, memo_type, content, scope_path, meta, created_at) "
               "VALUES ('tm1', 't', 'a', 'progress', 'c', '', '{}', '2026-01-01')")
    assert sqlite3.connect(path).execute("SELECT count(*) FROM sync_suppress").fetchone()[0] == 0
    assert [(t, kind) for t, _, kind, _ in _changes(path)] == [("task_memos", "I")]


async def test_startup_migrations_are_not_changes_and_silence_no_one(database) -> None:
    from aiteam.api.deps import _run_migrations

    repo, path = database
    team = await repo.create_team(name="old-team", mode="coordinate")
    agent = await repo.create_agent(team_id=team.id, name="old", role="member")
    _sql(path, f"UPDATE agents SET status = 'idle' WHERE id = '{agent.id}'", "DROP TRIGGER sync_i_reports")
    _capture(path)
    await _run_migrations(_url(path))  # the old 'idle' status becomes 'waiting'
    assert _sql_rows(path, "SELECT status FROM agents") == [("waiting",)]
    assert _changes(path) == []  # agents carries live triggers: suppressed, not missed
    assert _sql_rows(path, "SELECT count(*) FROM sync_suppress") == [(0,)]
    await init_db(_url(path))  # migrations end by reinstalling what a rebuild dropped
    assert _problems(path) == []
    _sql(path, f"UPDATE agents SET name = 'renamed' WHERE id = '{agent.id}'",
         "INSERT INTO reports (id, author, topic, report_type, date_str, content, created_at) "
         "VALUES ('r1', 'a', 't', 'r', 'd', 'c', '2026-01-01')")
    assert [(t, kind) for t, _, kind, _ in _changes(path)] == [("agents", "U"), ("reports", "I")]


def test_a_table_without_a_key_cannot_get_triggers() -> None:
    with pytest.raises(ValueError, match="no primary key"):
        changelog.trigger_sql("teams", [("name", 0), ("status", 0)])


async def test_reinstall_follows_schema_changes_and_drops_strays(database) -> None:
    _, path = database
    _sql(path, "ALTER TABLE memories ADD COLUMN later TEXT",
         "CREATE TRIGGER sync_i_notices AFTER INSERT ON notices BEGIN SELECT 1; END")
    assert _problems(path) == ["sync_u_memories: 定义与当前表结构不符", "sync_i_notices: 所在表不再同步"]
    await init_db(_url(path))
    assert _problems(path) == []
    engine = create_engine(f"sqlite:///{path}")
    with engine.begin() as conn:
        assert changelog.install_change_triggers(conn) == 0  # nothing left to do
    engine.dispose()


async def test_a_missing_state_row_never_fails_a_write(database) -> None:
    repo, path = database
    _sql(path, "DELETE FROM sync_state")
    await repo.create_project(name="p", root_path="/x")
    assert _changes(path) == []


def _presync(path: Path) -> None:
    """The shape a database had before sync existed (shared with the I32 check)."""
    spec = importlib.util.spec_from_file_location("check_sync_triggers", ROOT / "scripts" / "check_sync_triggers.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.strip_to_presync(path)


async def test_an_existing_database_upgrades_in_place(tmp_path: Path) -> None:
    path = tmp_path / "old.db"
    repo = StorageRepository(db_url=_url(path))
    await repo.init_db()
    project = await repo.create_project(name="kept", root_path="/kept")
    await close_db()
    _presync(path)

    await init_db(_url(path))
    await close_db()
    con = sqlite3.connect(path)
    try:
        assert con.execute("PRAGMA user_version").fetchone()[0] == UTC_CLOCK_MARKER  # never re-stamped
        assert con.execute("SELECT id, name, origin_node FROM projects").fetchall() == [(project.id, "kept", "")]
        assert con.execute("SELECT count(*) FROM sync_changes").fetchone()[0] == 0
    finally:
        con.close()
    assert _problems(path) == []
