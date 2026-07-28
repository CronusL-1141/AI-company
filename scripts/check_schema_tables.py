#!/usr/bin/env python3
"""I10 — schema table-set machine check (ORM declaration vs database on disk).

The 2026-07-28 D0 migration forensics found that the Mac database was built
fresh on 2026-07-06 and the Windows-era content never came with it. Nothing in
the project compares the schema the code believes in against the schema that
actually exists, so a table can fail to materialise — or vanish on the next
machine move — and stay invisible until someone reads it and finds it empty.

Two checks, deliberately asymmetric:

  * declared-but-absent  → FAIL. The application will query a table that is not
    there. This is the "silent table loss" this check exists to prevent.
  * present-but-undeclared → WARN. These are leftovers from retired features
    (``loop_states``, 198 rows from the retired cron engine). They are reported
    so they stay visible, but never fatal: historical tables are frozen and a
    hard failure would pressure exactly the wrong fix.

The live database is opened strictly read-only (``mode=ro``) — it is the user's
production data.

Usage: python3 scripts/check_schema_tables.py    (from the repo root)
Exit code: 0 = consistent (warnings do not block), 1 = declared table missing.
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Snapshot the caller's DB override before anything else: importing the models
# package forces a throwaway AITEAM_DB_PATH (below), which would otherwise make
# the checker resolve its own probe file as "the live database" and silently
# skip the half of the check that matters.
_ENV_DB_PATH = os.environ.get("AITEAM_DB_PATH")

# Bookkeeping tables that are not application schema: SQLite internals and the
# Alembic revision stamp. Never declared in the ORM, never worth reporting.
IGNORED_PREFIXES = ("sqlite_",)
IGNORED_DB_TABLES = frozenset({"alembic_version"})


def _is_bookkeeping(name: str) -> bool:
    return name in IGNORED_DB_TABLES or name.startswith(IGNORED_PREFIXES)


def compare(declared: set[str], actual: set[str]) -> tuple[list[str], list[str]]:
    """Compare the two table sets.

    Pure over the sets so the drift behaviour is unit-testable without a
    database. Returns ``(failures, warnings)``.
    """
    failures = [
        f"{name}: ORM 声明了该表，但数据库里不存在 —— 建表失败或换机丢表"
        for name in sorted(declared - actual)
    ]
    warnings = [
        f"{name}: 数据库有该表，但 ORM 未声明 —— 退役功能的遗留表（冻结不删，仅登记）"
        for name in sorted(actual - declared)
        if not _is_bookkeeping(name)
    ]
    return failures, warnings


def declared_tables() -> set[str]:
    """Every table registered on the ORM's declarative Base."""
    sys.path.insert(0, str(ROOT / "src"))
    # Point the DB path at a throwaway location before importing: module import
    # resolves DEFAULT_DB_URL eagerly and must never touch the real database.
    os.environ.setdefault("AITEAM_DB_PATH", str(Path(tempfile.gettempdir()) / "i10_probe.db"))
    from aiteam.storage.models import Base

    return set(Base.metadata.tables)


def creatable_tables() -> set[str]:
    """Tables that ``create_all`` actually materialises on an empty database.

    Catches a model that is declared but bound to a different Base — it would
    show up in neither the DDL nor, eventually, the database.
    """
    declared_tables()  # ensure the models module is imported
    from aiteam.storage.models import Base

    with tempfile.TemporaryDirectory() as tmp:
        from sqlalchemy import create_engine

        engine = create_engine(f"sqlite:///{Path(tmp) / 'ddl_probe.db'}")
        Base.metadata.create_all(engine)
        with engine.connect() as conn:
            rows = conn.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        engine.dispose()
    return {name for (name,) in rows if not _is_bookkeeping(name)}


def open_readonly(path: Path) -> sqlite3.Connection:
    """Open a SQLite file strictly read-only — production data is never written."""
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def live_db_path() -> Path:
    """Resolve the same database the OS uses (mirrors connection._default_db_url)."""
    if _ENV_DB_PATH:
        return Path(_ENV_DB_PATH).expanduser()
    return Path.home() / ".claude" / "data" / "ai-team-os" / "aiteam.db"


def db_tables(path: Path) -> set[str]:
    conn = open_readonly(path)
    try:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    finally:
        conn.close()
    return {name for (name,) in rows}


def main() -> int:
    declared = declared_tables()
    creatable = creatable_tables()

    failures: list[str] = []
    warnings: list[str] = []

    # Half 1 — always runs, including in CI where no live database exists.
    for name in sorted(declared - creatable):
        failures.append(f"{name}: ORM 声明了该表，但 create_all 不会建它（Base 注册表不一致）")

    # Half 2 — the live database, when this machine has one.
    path = live_db_path()
    # A freshly-created empty file (the 0-byte cwd probe) has no schema to compare.
    if path.is_file() and path.stat().st_size > 0:
        db_failures, warnings = compare(declared, db_tables(path))
        failures.extend(db_failures)
        checked = f"实库 {path}"
    else:
        checked = "实库缺失（CI 环境），仅校验 ORM↔DDL"

    for warning in warnings:
        print(f"⚠️  {warning}")
    if failures:
        for failure in failures:
            print(f"❌ {failure}")
        print(f"\n结论: ❌ 表集合漂移 {len(failures)} 处")
        return 1

    suffix = f"，另有 {len(warnings)} 张遗留表已登记" if warnings else ""
    print(f"✅ 表集合一致: ORM 声明 {len(declared)} 张 · {checked}{suffix}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
