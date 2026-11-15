#!/usr/bin/env python3
"""I32: every synced table carries its change-capture triggers.

Multi-host sync learns what changed from SQLite triggers (aiteam.sync.changelog).
A synced table without them changes silently: nothing fails, the other machine just
never hears of it. The checks:

1. The ORM, table by table (aiteam.sync.tables.schema_problems): every table is in
   exactly one group (session, shared, local, dead, sync), an unsynced one gives its
   reason, a synced one has a key to record changes under and the ``origin_node``
   column of OriginNodeMixin, no other table has it. A table nobody classified would
   neither sync nor be noticed; a model missing the mixin would only fail at run time.
2. A database built from nothing by ``init_db`` carries exactly the expected
   triggers (three per synced table, SQL equal to what the code generates, none on
   other tables) and the clock view, and ``origin_node`` exactly on the synced tables.
3. The same holds for a database shaped like one from before sync existed (no sync
   tables, no ``origin_node``, no triggers) after ``init_db`` upgrades it.
4. No SQL literal under src/aiteam writes a synced table with ``INSERT OR REPLACE``
   or ``REPLACE INTO``, and nothing uses ``prefix_with("OR REPLACE")``: SQLite drops
   the row a REPLACE displaces without firing a delete trigger, so its removal would
   never reach the change log.

With ``--db PATH`` the named database is checked instead, read-only. Without it the
live database, when this machine has one, is inspected too and reported as a warning
only: it gets its triggers when the API next starts with this code.

Usage: python3 scripts/check_sync_triggers.py [--db PATH]
Exit: 0 = consistent (warnings do not block), 1 = a check failed.
"""

from __future__ import annotations

import argparse
import ast
import asyncio
import os
import re
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_ENV_DB_PATH = os.environ.get("AITEAM_DB_PATH")
_ORIGIN = "origin_node"


def _problems(db_path: Path, *, readonly: bool) -> list[str]:
    from sqlalchemy import create_engine

    from aiteam.sync.changelog import trigger_problems
    from aiteam.sync.tables import SYNCED_TABLES

    url = f"sqlite:///file:{db_path}?mode=ro&uri=true" if readonly else f"sqlite:///{db_path}"
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            problems = trigger_problems(conn)
            tables = [row[0] for row in conn.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")]
            for table in sorted(tables):
                has_origin = any(row[1] == _ORIGIN for row in conn.exec_driver_sql(f'PRAGMA table_info("{table}")'))
                if has_origin != (table in SYNCED_TABLES):
                    problems.append(f"{table}: {'不该' if has_origin else '缺'} {_ORIGIN} 列")
    finally:
        engine.dispose()
    return problems


def _init(db_path: Path) -> None:
    from aiteam.storage.connection import close_db, init_db

    async def run() -> None:
        await init_db(f"sqlite+aiosqlite:///{db_path}")
        await close_db()

    asyncio.run(run())


def strip_to_presync(db_path: Path) -> None:
    """Take a current database back to the shape it had before sync existed."""
    from aiteam.sync.tables import SYNC_BOOKKEEPING_TABLES, SYNCED_TABLES

    con = sqlite3.connect(db_path)
    try:
        for (name,) in con.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'").fetchall():
            con.execute(f'DROP TRIGGER "{name}"')
        con.execute('DROP VIEW IF EXISTS "sync_next_hlc"')
        for table in SYNC_BOOKKEEPING_TABLES:
            con.execute(f'DROP TABLE IF EXISTS "{table}"')
        for table in SYNCED_TABLES:
            con.execute(f'ALTER TABLE "{table}" DROP COLUMN {_ORIGIN}')
        con.commit()
    finally:
        con.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, help="check this database (read-only) instead of building one")
    args = parser.parse_args()

    sys.path.insert(0, str(ROOT / "src"))
    failures: list[str] = []
    warnings: list[str] = []
    with tempfile.TemporaryDirectory(prefix="i32-") as workdir:
        # Importing the storage layer resolves a database path: keep it off the real one.
        os.environ["AITEAM_DB_PATH"] = str(Path(workdir) / "probe.db")
        from aiteam.storage.models import Base
        from aiteam.sync.tables import SYNCED_TABLES, schema_problems

        failures += schema_problems(Base.metadata.tables)
        failures += replace_writes(ROOT / "src" / "aiteam", SYNCED_TABLES)
        if args.db:
            failures += [f"{args.db}: {p}" for p in _problems(args.db, readonly=True)]
            checked = f"指定库 {args.db}"
        else:
            fresh = Path(workdir) / "fresh.db"
            _init(fresh)
            failures += [f"新建库: {p}" for p in _problems(fresh, readonly=False)]
            upgraded = Path(workdir) / "upgraded.db"
            _init(upgraded)
            strip_to_presync(upgraded)
            _init(upgraded)
            failures += [f"升级库: {p}" for p in _problems(upgraded, readonly=False)]
            checked = "新建库与升级库"
            live = Path(_ENV_DB_PATH).expanduser() if _ENV_DB_PATH else _live_db_path()
            if live is not None and live.is_file() and live.stat().st_size > 0:
                live_problems = _problems(live, readonly=True)
                if live_problems:
                    warnings.append(f"实库 {live} 有 {len(live_problems)} 处与代码不符，首条「{live_problems[0]}」"
                                    "（API 以本代码重启后由启动迁移补齐）")

    for warning in warnings:
        print(f"⚠️  {warning}")
    if failures:
        print("\n".join(f"❌ {failure}" for failure in failures))
        return 1
    print(f"{len(SYNCED_TABLES)} 张同步表各有 3 个变更触发器与 {_ORIGIN} 列，可合并表有字段时钟、自然键表有墓碑"
          f"触发器，时钟视图在（{checked}），全部 ORM 表已分组，无 REPLACE 写同步表")
    return 0


_REPLACE = re.compile(r"\b(?:INSERT\s+OR\s+REPLACE|REPLACE)\s+INTO\s+[\"'`\[]?(\w+)", re.IGNORECASE)


def replace_writes(src: Path, synced: frozenset[str]) -> list[str]:
    """REPLACE writes to synced tables: string literals, and any ``prefix_with`` REPLACE."""
    problems = []
    for path in sorted(src.rglob("*.py")):
        if "hooks" in path.relative_to(src).parts:
            continue  # hook scripts never open the database
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            where = f"{path.relative_to(ROOT)}:{getattr(node, 'lineno', '?')}"
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                for table in _REPLACE.findall(node.value):
                    if table in synced:
                        problems.append(f"{where}: REPLACE 写同步表 {table}，被顶掉的旧行不会进变更日志")
            elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                  and node.func.attr == "prefix_with"
                  and any(isinstance(a, ast.Constant) and "REPLACE" in str(a.value).upper() for a in node.args)):
                problems.append(f"{where}: prefix_with REPLACE，被顶掉的旧行不会进变更日志")
    return problems


def _live_db_path() -> Path | None:
    from aiteam.paths import DB_NAME, resolve_data_dir

    resolution = resolve_data_dir()
    return None if resolution.mode == "missing" else resolution.path / DB_NAME


if __name__ == "__main__":
    sys.exit(main())
