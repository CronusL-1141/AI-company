"""时钟标记 ``PRAGMA user_version = 20260728`` 的落点，规则见 docs/utc-unification-design.md §6.7。

标记说的是「这个库的核心域时间戳按 UTC 存」。恢复脚本据它决定要不要换算，平移脚本据它
拒绝二次平移，所以它只能落在确知为 UTC 的库上：

* ``init_db`` 从零建表的库天然是 UTC，建表时打标记；已有任何表的库（存量旧库可能是本地
  墙钟）不论标记是多少都不碰，否则恢复与迁移会把本地墙钟当成 UTC；多个进程同时首启同一个
  新库时，只有拿到写锁后复查仍为空的那一个建表并打标记；
* 平移脚本在「无需平移」时早退，``--apply`` 也写标记，dry-run 不写，两者都写明「值由本机
  时钟写入」的前提；已带标记的库 dry-run 不再提议位移。

断言一律用新开的 sqlite3 连接读回。
"""

from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine

from aiteam.services import attribution_repair
from aiteam.storage import connection
from aiteam.storage.connection import close_db
from aiteam.storage.models import Base
from aiteam.storage.repository import StorageRepository

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "migrate_timestamps_utc.py"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


mig = _load("migrate_timestamps_utc_marker", SCRIPT)
backfill = _load("codex_backfill_marker", ROOT / "scripts" / "backfill_codex_usage.py")
MARKER = mig.MIGRATION_MARKER


def _marker(path: Path) -> int:
    with sqlite3.connect(path) as con:
        return con.execute("pragma user_version").fetchone()[0]


def _tables(path: Path) -> set[str]:
    with sqlite3.connect(path) as con:
        return {r[0] for r in con.execute("select name from sqlite_master where type='table'")}


async def _init(path: Path) -> None:
    try:
        await StorageRepository(db_url=f"sqlite+aiosqlite:///{path}").init_db()
    finally:
        await close_db()


def test_marker_value_is_one_fact_in_four_places() -> None:
    """四处各存一份同一个值（脚本与 attribution_repair 不 import storage：那会在导入时解析默认库路径）。"""
    assert (connection.UTC_CLOCK_MARKER == mig.MIGRATION_MARKER == backfill.UTC_MARKER
            == attribution_repair.UTC_CLOCK_MARKER == 20260728)


# ---------------------------------------------------------------------------
# init_db
# ---------------------------------------------------------------------------
async def test_database_built_from_nothing_is_born_utc_marked(tmp_path: Path) -> None:
    db = tmp_path / "fresh.db"
    await _init(db)
    assert _marker(db) == MARKER
    assert {"agents", "projects", "tasks"} <= _tables(db)
    await _init(db)  # the second start sees an existing schema and leaves the marker alone
    assert _marker(db) == MARKER


def _legacy_full_schema(path: Path, marker: int) -> None:
    """What an older init_db left behind: the whole schema, no marker, a local wall-clock row."""
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    engine.dispose()
    with sqlite3.connect(path) as con:
        con.execute(
            "insert into projects (id, name, description, root_path, config, created_at, updated_at) "
            "values ('p', 'legacy', '', '', '{}', '2026-07-01 17:00:00.000000', '2026-07-01 17:00:00.000000')"
        )
        con.execute(f"pragma user_version = {marker}")


def _one_unrelated_table(path: Path, marker: int) -> None:
    with sqlite3.connect(path) as con:
        con.execute("create table legacy_notes (id integer primary key, written_at text)")
        con.execute("insert into legacy_notes (written_at) values ('2026-07-01 17:00:00')")
        con.execute(f"pragma user_version = {marker}")


def _empty_with_foreign_marker(path: Path, marker: int) -> None:
    with sqlite3.connect(path) as con:
        con.execute(f"pragma user_version = {marker}")


@pytest.mark.parametrize(
    ("build", "marker"),
    [
        (_legacy_full_schema, 0),
        (_one_unrelated_table, 0),
        (_legacy_full_schema, 20260728),
        (_empty_with_foreign_marker, 7),
    ],
    ids=["legacy-schema-unmarked", "one-unrelated-table-unmarked", "migrated-utc", "empty-foreign-marker"],
)
async def test_existing_database_keeps_whatever_marker_it_had(tmp_path: Path, build, marker: int) -> None:
    """Any table at all means the file predates this start; its clock is not ours to declare."""
    db = tmp_path / "existing.db"
    build(db, marker)
    await _init(db)
    assert _marker(db) == marker
    assert {"agents", "projects"} <= _tables(db)  # init_db still did its normal job
    if build is _legacy_full_schema:
        with sqlite3.connect(db) as con:
            assert con.execute("select created_at from projects where id='p'").fetchone()[0] == (
                "2026-07-01 17:00:00.000000"
            )


async def test_failed_first_build_leaves_neither_half_schema_nor_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A half-built schema without the marker would never be recognised as fresh again."""
    db = tmp_path / "crash.db"

    def crash_midway(sync_conn, *args, **kwargs):
        sync_conn.exec_driver_sql("create table half_built (id integer primary key)")
        raise RuntimeError("simulated crash during the first build")

    monkeypatch.setattr(Base.metadata, "create_all", crash_midway)
    with pytest.raises(RuntimeError, match="simulated crash"):
        await _init(db)
    assert _tables(db) == set()
    assert _marker(db) == 0
    monkeypatch.undo()
    await _init(db)
    assert _marker(db) == MARKER


# Each child gates its first emptiness check on stdin, so the parent can release all of
# them only after every one has seen the empty file: they then all reach BEGIN IMMEDIATE
# believing the database is new, and only the recheck under the write lock tells them
# apart. Every statement that writes user_version is counted on the engine itself.
FIRST_START_CHILD = r"""
import asyncio
import re
import sys

from sqlalchemy import event

from aiteam.storage import connection

url = sys.argv[1]
first_check = connection._unmarked_empty_database
seen = []


def gated(sync_conn):
    result = first_check(sync_conn)
    if not seen:
        seen.append(result)
        print("checked", result, flush=True)
        sys.stdin.readline()
    return result


connection._unmarked_empty_database = gated
stamp = re.compile(r"\s*PRAGMA\s+user_version\s*=", re.IGNORECASE)
stamps = []


@event.listens_for(connection.get_engine(url).sync_engine, "before_cursor_execute")
def count_stamps(conn, cursor, statement, parameters, context, executemany):
    if stamp.match(statement):
        stamps.append(statement)


async def main():
    try:
        await connection.init_db(url)
    finally:
        await connection.close_db()


asyncio.run(main())
print("stamps", len(stamps), flush=True)
"""
FIRST_STARTS = 4


def test_concurrent_first_starts_build_and_mark_the_database_once(tmp_path: Path) -> None:
    """Separate processes starting on one new file: all succeed, one builds and marks it."""
    db = tmp_path / "first-start.db"
    env = {
        **os.environ,
        "PYTHONPATH": str(ROOT / "src"),
        "HOME": str(tmp_path / "home"),
        "AITEAM_DB_PATH": str(db),
    }
    logs = [tmp_path / f"child-{i}.log" for i in range(FIRST_STARTS)]
    children: list[subprocess.Popen] = []
    try:
        for log in logs:
            with log.open("w") as err:
                children.append(
                    subprocess.Popen(
                        [sys.executable, "-c", FIRST_START_CHILD, f"sqlite+aiosqlite:///{db}"],
                        stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE,
                        stderr=err,
                        text=True,
                        env=env,
                        cwd=tmp_path,
                    )
                )
        checks = [child.stdout.readline().strip() for child in children]  # type: ignore[union-attr]
        assert checks == ["checked True"] * FIRST_STARTS, [log.read_text() for log in logs]
        outputs = [child.communicate("go\n", timeout=60)[0] for child in children]
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
                child.wait()
    failures = [log.read_text() for child, log in zip(children, logs, strict=True) if child.returncode != 0]
    assert not failures, failures
    stamps = [int(out.split()[-1]) for out in outputs]
    assert sum(stamps) == 1, stamps
    assert _marker(db) == MARKER
    assert set(Base.metadata.tables) <= _tables(db)
    with sqlite3.connect(db) as con:
        assert con.execute("pragma integrity_check").fetchone()[0] == "ok"


# ---------------------------------------------------------------------------
# migrate_timestamps_utc.py: the "no shift needed" exit
# ---------------------------------------------------------------------------
def _events_db(path: Path, stamps: list[str]) -> None:
    with sqlite3.connect(path) as con:
        con.execute("create table events (id integer primary key, timestamp text)")
        con.executemany("insert into events (timestamp) values (?)", [(s,) for s in stamps])


def _dump(path: Path) -> list[tuple]:
    with sqlite3.connect(path) as con:
        return list(con.execute("select rowid, timestamp from events order by rowid"))


def _run(*argv: str, tz: str | None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    if tz is not None:
        env["TZ"] = tz
    return subprocess.run(
        [sys.executable, str(SCRIPT), *argv], env=env, capture_output=True, text=True, timeout=60, check=False
    )


def _utc_stamps(*minutes_ago: int) -> list[str]:
    now = datetime.now(UTC).replace(tzinfo=None)
    return [(now - timedelta(minutes=m)).strftime("%Y-%m-%d %H:%M:%S.%f") for m in minutes_ago]


def _assert_states_the_local_clock_premise(out: str) -> None:
    """Zero offset only means UTC if this host's clock wrote the values, and the one
    available counter-evidence (a birth time ahead of UTC now) only exposes eastern zones."""
    assert "前提" in out and "本机时钟" in out, out
    assert "东区" in out and "西区" in out, out


def test_utc_host_apply_writes_the_marker_and_dry_run_does_not(tmp_path: Path) -> None:
    """TZ=UTC 在这里是被测场景（UTC 宿主），不是绕开本机时区。"""
    db = tmp_path / "utc-host.db"
    _events_db(db, _utc_stamps(30, 20, 10))
    before = _dump(db)

    dry = _run("--db", str(db), tz="UTC")
    assert dry.returncode == 0, dry.stderr
    assert "无需平移" in dry.stdout
    _assert_states_the_local_clock_premise(dry.stdout)
    assert _marker(db) == 0

    journal = tmp_path / "j.json"
    applied = _run("--db", str(db), "--apply", "--journal", str(journal), tz="UTC")
    assert applied.returncode == 0, applied.stdout + applied.stderr
    _assert_states_the_local_clock_premise(applied.stdout)
    assert _marker(db) == MARKER
    assert _dump(db) == before  # no value moved
    record = json.loads(journal.read_text())
    assert (record["mode"], record["shift_hours"], record["marker"]) == ("apply", 0.0, MARKER)

    again = _run("--db", str(db), "--apply", "--journal", str(tmp_path / "j2.json"), tz="UTC")
    assert again.returncode == 3
    assert not (tmp_path / "j2.json").exists()


def test_utc_host_refuses_to_mark_values_written_by_a_clock_ahead_of_utc(tmp_path: Path) -> None:
    """A UTC clock cannot write a birth time in the future: such a database came from another zone."""
    db = tmp_path / "moved.db"
    now = datetime.now(UTC).replace(tzinfo=None)
    _events_db(db, [(now + timedelta(hours=8, minutes=-m)).strftime("%Y-%m-%d %H:%M:%S.%f") for m in (30, 20)])
    journal = tmp_path / "j.json"
    result = _run("--db", str(db), "--apply", "--journal", str(journal), tz="UTC")
    assert result.returncode == 3, result.stdout + result.stderr
    assert _marker(db) == 0
    assert not journal.exists()


def test_database_without_any_time_value_is_marked_on_apply_whatever_the_host_zone(tmp_path: Path) -> None:
    """No core-domain value exists to be in any other clock; the host zone does not matter here."""
    db = tmp_path / "empty.db"
    _events_db(db, [])
    dry = _run("--db", str(db), tz=None)
    assert dry.returncode == 0, dry.stderr
    assert _marker(db) == 0
    journal = tmp_path / "j.json"
    applied = _run("--db", str(db), "--apply", "--journal", str(journal), tz=None)
    assert applied.returncode == 0, applied.stdout + applied.stderr
    assert _marker(db) == MARKER
    assert json.loads(journal.read_text())["shift_hours"] == 0.0


async def test_dry_run_on_a_marked_database_proposes_no_shift(tmp_path: Path) -> None:
    """Review repro: a database born marked, holding one UTC row, still got a whole-table
    shift proposal (+7 hours in PDT) in dry-run although --apply refuses it."""
    db = tmp_path / "born.db"
    await _init(db)
    (stamp,) = _utc_stamps(10)
    with sqlite3.connect(db) as con:
        con.execute(
            "insert into projects (id, name, description, root_path, config, created_at, updated_at) "
            "values ('p', 'born', '', '', '{}', ?, ?)",
            (stamp, stamp),
        )
    journal = tmp_path / "dry.json"
    dry = _run("--db", str(db), "--journal", str(journal), tz=None)
    assert dry.returncode == 0, dry.stdout + dry.stderr
    assert "已带 UTC 标记" in dry.stdout
    assert "存量时间戳平移" not in dry.stdout  # the per-table shift proposal
    assert "--apply 会写入" not in dry.stdout
    assert not journal.exists()  # nothing to review
    assert _marker(db) == MARKER
    with sqlite3.connect(db) as con:
        assert con.execute("select created_at from projects where id='p'").fetchone()[0] == stamp


def test_dry_run_calls_a_foreign_marker_taken_not_migrated(tmp_path: Path) -> None:
    """A user_version other than the UTC marker says nothing about the clock; --apply refuses it."""
    db = tmp_path / "foreign.db"
    now = datetime.now()
    _events_db(db, [(now - timedelta(minutes=m)).strftime("%Y-%m-%d %H:%M:%S.%f") for m in (30, 20)])
    with sqlite3.connect(db) as con:
        con.execute("pragma user_version = 7")
    dry = _run("--db", str(db), tz=None)
    assert dry.returncode == 0, dry.stdout + dry.stderr
    assert "被别的用途占用" in dry.stdout
    assert "表示已平移过" not in dry.stdout
    assert "--apply 会写入" not in dry.stdout
    applied = _run("--db", str(db), "--apply", "--journal", str(tmp_path / "j.json"), tz=None)
    assert applied.returncode == 3, applied.stdout + applied.stderr
    assert _marker(db) == 7
