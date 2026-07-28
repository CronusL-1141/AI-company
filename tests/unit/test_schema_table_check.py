"""I10 machine check — ORM-declared tables vs the tables the live DB actually has.

Born from the 2026-07-28 D0 migration forensics: the Mac database was created
fresh on 2026-07-06 and nobody noticed that the Windows-era content never came
across, because nothing compares the schema the code believes in against the
schema on disk. A table that silently fails to materialise (or quietly
disappears on the next machine move) is invisible until someone reads a table
and finds it empty.

Direction matters and the two directions are not symmetric:

  * declared-but-absent  → loss. The code will query a table that isn't there.
  * present-but-undeclared → leftovers (``loop_states`` from the retired cron
    engine). Reporting them is useful, failing on them is not — the D0 freeze
    order forbids deleting historical tables, so a hard failure would push
    exactly the wrong way.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _load_checker():
    spec = importlib.util.spec_from_file_location(
        "check_schema_tables", ROOT / "scripts" / "check_schema_tables.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


checker = _load_checker()


def test_identical_sets_are_clean():
    failures, warnings = checker.compare({"tasks", "agents"}, {"tasks", "agents"})
    assert failures == []
    assert warnings == []


def test_declared_but_missing_is_a_failure():
    failures, warnings = checker.compare({"tasks", "meetings"}, {"tasks"})
    assert len(failures) == 1
    assert "meetings" in failures[0]
    assert warnings == []


def test_every_missing_table_is_named():
    failures, _ = checker.compare({"a", "b", "c"}, {"a"})
    assert len(failures) == 2
    assert {f.split(":")[0] for f in failures} == {"b", "c"}


def test_undeclared_table_warns_but_does_not_fail():
    """Orphan tables are reported, never fatal — the freeze order forbids deleting them."""
    failures, warnings = checker.compare({"tasks"}, {"tasks", "loop_states"})
    assert failures == []
    assert len(warnings) == 1
    assert "loop_states" in warnings[0]


def test_bookkeeping_tables_are_ignored_entirely():
    """alembic/sqlite internals are not application schema and must stay silent."""
    actual = {"tasks", "alembic_version", "sqlite_sequence", "sqlite_stat1"}
    failures, warnings = checker.compare({"tasks"}, actual)
    assert failures == []
    assert warnings == []


def test_orm_metadata_is_non_empty():
    """A broken import would yield an empty set and make the check vacuously pass."""
    declared = checker.declared_tables()
    assert len(declared) > 20
    assert "tasks" in declared and "meetings" in declared


def test_declared_tables_all_materialise_via_create_all():
    """The ORM registry and the DDL the app actually runs must agree.

    CI has no live database, so this is the half of I10 that always runs: it
    catches a model that is declared but bound to a different Base and would
    therefore never be created.
    """
    assert checker.creatable_tables() == checker.declared_tables()


def test_importing_models_does_not_hijack_the_live_db_path():
    """The import-time probe path must not become "the live database".

    declared_tables() forces AITEAM_DB_PATH so importing the models package
    cannot touch real data; if live_db_path() then read that same variable the
    checker would compare the ORM against its own empty probe file and report a
    clean bill of health while never looking at the real database.
    """
    checker.declared_tables()
    assert "i10_probe.db" not in str(checker.live_db_path())


def test_live_db_is_opened_read_only():
    """Production DB is read-only by iron rule — the checker must not be able to write."""
    import sqlite3

    import pytest

    path = ROOT / "tests" / "_i10_ro_probe.db"
    sqlite3.connect(path).execute("create table if not exists probe (x int)")
    try:
        conn = checker.open_readonly(path)
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("create table nope (x int)")
        conn.close()
    finally:
        path.unlink(missing_ok=True)
