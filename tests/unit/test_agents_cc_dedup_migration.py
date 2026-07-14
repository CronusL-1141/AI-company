"""Regression — B1: agents.cc_tool_use_id partial-unique migration on existing DBs.

Covers connection._ensure_agents_cc_tool_use_id_unique: dedup pre-existing rows that
share a non-null cc_tool_use_id BEFORE building the partial unique index, keeping the
most-complete/newest row, and — the round-35 blood lesson — NEVER deleting a
role='leader' row. Uses stdlib sqlite3 to build an `agents` table WITHOUT the index
(SQLAlchemy create_all would pre-create it and block inserting the duplicates we test).
"""

from __future__ import annotations

import sqlite3

from aiteam.storage.connection import _ensure_agents_cc_tool_use_id_unique

# Minimal subset of the real agents schema — only the columns the migration reads.
_DDL = (
    "CREATE TABLE agents ("
    "  id TEXT PRIMARY KEY,"
    "  role TEXT,"
    "  status TEXT,"
    "  current_task TEXT,"
    "  model TEXT,"
    "  last_active_at TEXT,"
    "  created_at TEXT,"
    "  cc_tool_use_id TEXT"
    ")"
)


def _mk_db() -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    con.execute(_DDL)
    return con


def _insert(con: sqlite3.Connection, **cols: object) -> None:
    keys = ", ".join(cols)
    marks = ", ".join("?" for _ in cols)
    con.execute(f"INSERT INTO agents ({keys}) VALUES ({marks})", tuple(cols.values()))


def _index_exists(con: sqlite3.Connection) -> bool:
    rows = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='index' "
        "AND name='uq_agents_cc_tool_use_id'"
    ).fetchall()
    return bool(rows)


def _ids(con: sqlite3.Connection) -> set[str]:
    return {r[0] for r in con.execute("SELECT id FROM agents").fetchall()}


def test_dedup_removes_nonleader_duplicates_and_builds_index():
    con = _mk_db()
    # Two members share cc='X'; the newer/most-complete one must survive.
    _insert(
        con, id="old", role="workflow-subagent", status="offline",
        current_task=None, model="", last_active_at="2026-07-14 10:00:00",
        created_at="2026-07-14 09:00:00", cc_tool_use_id="X",
    )
    _insert(
        con, id="new", role="workflow-subagent", status="busy",
        current_task="doing", model="claude-x", last_active_at="2026-07-14 12:00:00",
        created_at="2026-07-14 11:00:00", cc_tool_use_id="X",
    )
    # An unrelated single member (cc='Y') must be untouched.
    _insert(
        con, id="solo", role="workflow-subagent", status="busy",
        current_task=None, model="", last_active_at="2026-07-14 12:00:00",
        created_at="2026-07-14 11:30:00", cc_tool_use_id="Y",
    )
    con.commit()

    _ensure_agents_cc_tool_use_id_unique(con)

    # 'old' removed, 'new' kept (most-complete/newest), 'solo' untouched.
    assert _ids(con) == {"new", "solo"}
    assert _index_exists(con)
    # Index now enforces the constraint: a third insert of cc='X' must raise.
    try:
        _insert(
            con, id="dup", role="workflow-subagent", status="busy",
            current_task=None, model="", last_active_at="2026-07-14 13:00:00",
            created_at="2026-07-14 13:00:00", cc_tool_use_id="X",
        )
        con.commit()
        raised = False
    except sqlite3.IntegrityError:
        raised = True
    assert raised, "partial unique index should reject a duplicate cc_tool_use_id"


def test_dedup_never_deletes_leader_row():
    con = _mk_db()
    # A leader carrying NULL cc (the normal topology) coexists with a member dup group.
    _insert(
        con, id="leader", role="leader", status="busy", current_task=None,
        model="", last_active_at="2026-07-14 12:00:00",
        created_at="2026-07-14 08:00:00", cc_tool_use_id=None,
    )
    _insert(
        con, id="m1", role="workflow-subagent", status="offline", current_task=None,
        model="", last_active_at="2026-07-14 10:00:00",
        created_at="2026-07-14 09:00:00", cc_tool_use_id="Z",
    )
    _insert(
        con, id="m2", role="workflow-subagent", status="busy", current_task="x",
        model="m", last_active_at="2026-07-14 12:00:00",
        created_at="2026-07-14 11:00:00", cc_tool_use_id="Z",
    )
    con.commit()

    _ensure_agents_cc_tool_use_id_unique(con)

    # Leader always survives; the member dup group collapses to the newer row.
    assert "leader" in _ids(con)
    assert _ids(con) == {"leader", "m2"}
    assert _index_exists(con)
    # NULL cc rows stay exempt — a second NULL-cc leader must still insert fine.
    _insert(
        con, id="leader2", role="leader", status="busy", current_task=None,
        model="", last_active_at="2026-07-14 12:00:00",
        created_at="2026-07-14 08:30:00", cc_tool_use_id=None,
    )
    con.commit()
    assert "leader2" in _ids(con)


def test_dedup_is_idempotent():
    con = _mk_db()
    _insert(
        con, id="a", role="workflow-subagent", status="busy", current_task=None,
        model="", last_active_at="2026-07-14 10:00:00",
        created_at="2026-07-14 09:00:00", cc_tool_use_id="Q",
    )
    _insert(
        con, id="b", role="workflow-subagent", status="busy", current_task=None,
        model="", last_active_at="2026-07-14 11:00:00",
        created_at="2026-07-14 10:00:00", cc_tool_use_id="Q",
    )
    con.commit()

    _ensure_agents_cc_tool_use_id_unique(con)
    after_first = _ids(con)
    # Second run is a no-op (index already there, no dup groups left).
    _ensure_agents_cc_tool_use_id_unique(con)
    assert _ids(con) == after_first
    assert len(after_first) == 1
    assert _index_exists(con)
