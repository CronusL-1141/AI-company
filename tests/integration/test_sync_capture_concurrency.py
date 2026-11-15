"""Change capture across processes: a migration elsewhere never silences this node's writes.

Suppression lives only inside the transaction of the writer that suppresses. Two
processes on one database prove it, with capture on as on a syncing node:

* another process runs ``init_db`` and the startup migrations (a CLI, a maintenance
  script, a second API starting) while this one writes: a write during ``init_db`` is
  in the change log, and while the migrations suppress capture they hold the write
  lock, so no write can happen unseen;
* a process killed inside the suppressed migration transaction leaves no marker
  behind: the rollback takes it, and the next write is recorded.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from aiteam.storage.connection import close_db
from aiteam.storage.repository import StorageRepository

ROOT = Path(__file__).resolve().parents[2]

# Another process starting on the same database, paused at two points: inside init_db's
# column migrations, and inside the suppressed transaction of the startup migrations.
MIGRATE_WITH_PAUSES = r"""
import asyncio, sys
from sqlalchemy import event
from aiteam.api import deps
from aiteam.storage import connection

url = sys.argv[1]

def pause(where):
    print(where, flush=True)
    sys.stdin.readline()

original = connection._sqlite_migrate

def paused_migrate(db_path):
    pause("init_db")
    original(db_path)

connection._sqlite_migrate = paused_migrate

@event.listens_for(connection.get_engine(url).sync_engine, "before_cursor_execute")
def hold(conn, cursor, statement, parameters, context, executemany):
    if statement.startswith("UPDATE agents SET status='waiting'"):
        pause("migration")

async def main():
    await connection.init_db(url)
    await deps._run_migrations(url)
    await connection.close_db()

asyncio.run(main())
"""

DIE_INSIDE_THE_MIGRATION = r"""
import asyncio, os, sys
from sqlalchemy import event
from aiteam.api.deps import _run_migrations
from aiteam.storage.connection import get_engine

@event.listens_for(get_engine(sys.argv[1]).sync_engine, "before_cursor_execute")
def die(conn, cursor, statement, parameters, context, executemany):
    if statement.startswith("UPDATE agents SET status='waiting'"):
        os._exit(9)  # after the suppression marker, before the transaction commits

asyncio.run(_run_migrations(sys.argv[1]))
"""


def _env() -> dict[str, str]:
    return {**os.environ, "PYTHONPATH": str(ROOT / "src")}


async def _node(path: Path) -> str:
    """A capturing node's database with one agent still on the retired 'idle' status."""
    repo = StorageRepository(db_url=f"sqlite+aiosqlite:///{path}")
    await repo.init_db()
    team = await repo.create_team(name="t", mode="coordinate")
    agent = await repo.create_agent(team_id=team.id, name="old", role="member")
    await close_db()
    con = sqlite3.connect(path)
    con.execute("UPDATE agents SET status = 'idle' WHERE id = ?", (agent.id,))
    con.execute("UPDATE sync_state SET capture = 3, node_id = 'air-3f9c'")
    con.commit()
    con.close()
    return agent.id


def _write_report(con: sqlite3.Connection, n: int) -> None:
    con.execute("INSERT INTO reports (id, author, topic, report_type, date_str, content, created_at) "
                "VALUES (?, 'a', 't', 'r', 'd', 'c', '2026-01-01')", (f"r{n}",))
    con.commit()


def _recorded_reports(path: Path) -> int:
    con = sqlite3.connect(path)
    try:
        return con.execute(
            "SELECT count(*) FROM sync_changes WHERE table_name = 'reports' AND kind = 'I'").fetchone()[0]
    finally:
        con.close()


async def test_migrations_elsewhere_never_silence_a_write(tmp_path: Path) -> None:
    path = tmp_path / "node.db"
    agent_id = await _node(path)
    other = subprocess.Popen([sys.executable, "-c", MIGRATE_WITH_PAUSES, f"sqlite+aiosqlite:///{path}"],
                             env=_env(), cwd=tmp_path, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True)
    con = sqlite3.connect(path, timeout=0)
    try:
        assert other.stdout.readline().strip() == "init_db"
        _write_report(con, 0)  # while the other process runs init_db's migrations
        other.stdin.write("\n")
        other.stdin.flush()
        assert other.stdout.readline().strip() == "migration"
        # The suppressing transaction holds the write lock: nothing can be written unseen meanwhile.
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            _write_report(con, 1)
        con.rollback()
        other.stdin.write("\n")
        other.stdin.flush()
        _, stderr = other.communicate(timeout=120)
        assert other.returncode == 0, stderr[-3000:]
        _write_report(con, 2)
        assert con.execute("SELECT status FROM agents WHERE id = ?", (agent_id,)).fetchone()[0] == "waiting"
        assert con.execute("SELECT count(*) FROM sync_suppress").fetchone()[0] == 0
    finally:
        con.close()
        if other.poll() is None:
            other.kill()
            other.wait()
    assert _recorded_reports(path) == 2


async def test_a_migration_killed_midway_leaves_capture_on(tmp_path: Path) -> None:
    path = tmp_path / "node.db"
    agent_id = await _node(path)
    killed = subprocess.run([sys.executable, "-c", DIE_INSIDE_THE_MIGRATION, f"sqlite+aiosqlite:///{path}"],
                            env=_env(), cwd=tmp_path, capture_output=True, text=True, timeout=120)
    assert killed.returncode == 9, killed.stderr[-3000:]
    con = sqlite3.connect(path, timeout=30)
    try:
        assert con.execute("SELECT count(*) FROM sync_suppress").fetchone()[0] == 0
        assert con.execute("SELECT status FROM agents WHERE id = ?", (agent_id,)).fetchone()[0] == "idle"
        _write_report(con, 0)
    finally:
        con.close()
    assert _recorded_reports(path) == 1

