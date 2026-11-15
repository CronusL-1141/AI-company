"""Change capture: SQLite triggers that log every write to a synced table.

Triggers rather than repository hooks, because not every write goes through the
repository: startup migrations, maintenance scripts and raw SQL write tables
directly, and a trigger sees them all, deletes included.

Each synced table gets three triggers (``sync_i_<t>``, ``sync_u_<t>``,
``sync_d_<t>``) appending ``(table_name, pk, kind, hlc)`` to ``sync_changes``; the
clock value comes from the view ``sync_next_hlc``, one definition for all of them.
They record only while

* ``sync_state.capture`` is on: a node that syncs. A standalone node keeps the
  triggers but records nothing, so its database grows by nothing and holds
  exactly what it held before; and
* ``sync_suppress`` is empty. A writer whose writes are not changes to ship (a
  sync applier, a startup migration that runs in one transaction) inserts its row
  as the first write of its own transaction and deletes it before committing
  (``suppressed``). SQLite has one writer at a time, so no other connection ever
  sees the row, and a process killed mid-transaction rolls it back. A committed
  marker would silence every other writer of the database while it stands, and a
  killed holder would leave it standing: work that cannot run in one transaction is
  not suppressed at all. Recording too much is harmless, the replay is idempotent;
  recording too little loses data.

A trigger must never fail the write it observes: every value it reads is
coalesced, so a missing ``sync_state`` row means "do not record", not an error.

Triggers are reinstalled after every startup migration (``install_change_triggers``):
a migration may rebuild a table, which drops its triggers, or add a column, which
changes the column list an update trigger watches. Installation compares the stored
SQL and only touches objects that differ.

The node's clock state is the last row of ``sync_changes``: pruning must keep it.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine

from aiteam.sync.hlc import tick_sql
from aiteam.sync.tables import LOCAL_COLUMNS, SYNCED_TABLES, sync_key

MIGRATION_HOLDER = "migration"
APPLIER_HOLDER = "applier"

CLOCK_VIEW = "sync_next_hlc"
_KINDS = (("i", "I", "INSERT", "NEW"), ("u", "U", "UPDATE", "NEW"), ("d", "D", "DELETE", "OLD"))
_OUR_TRIGGER = re.compile(r"^sync_[iud]_")

_PHYSICAL_MS = "CAST((julianday('now') - 2440587.5) * 86400000 AS INTEGER)"
_LAST_CHANGE = "(SELECT hlc FROM sync_changes ORDER BY seq DESC LIMIT 1)"
_NODE = "(SELECT node_id FROM sync_state WHERE id = 1)"
_RECORDING = (
    "coalesce((SELECT capture FROM sync_state WHERE id = 1), 0) "
    "AND NOT EXISTS (SELECT 1 FROM sync_suppress)"
)
CLOCK_VIEW_SQL = f'CREATE VIEW "{CLOCK_VIEW}" AS {tick_sql(_LAST_CHANGE, _NODE, _PHYSICAL_MS)}'


def trigger_name(kind: str, table: str) -> str:
    return f"sync_{kind}_{table}"


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _key_sql(alias: str, columns: tuple[str, ...]) -> str:
    if len(columns) == 1:
        return f"coalesce(CAST({alias}.{_quote(columns[0])} AS TEXT), '')"
    return "json_array(" + ", ".join(f"{alias}.{_quote(c)}" for c in columns) + ")"


def _record(table: str, key_sql: str, kind: str, condition: str = "") -> str:
    where = f" WHERE {condition}" if condition else ""
    return (
        f"INSERT INTO sync_changes (table_name, pk, kind, hlc) "
        f"SELECT '{table}', {key_sql}, '{kind}', hlc FROM {_quote(CLOCK_VIEW)}{where};"
    )


def _table_columns(conn: Connection, table: str) -> list[tuple[str, int]]:
    """(name, primary-key position) per column, in table order."""
    rows = conn.exec_driver_sql(f"PRAGMA table_info({_quote(table)})").all()
    return [(row[1], row[5]) for row in rows]


def trigger_sql(table: str, columns: list[tuple[str, int]]) -> dict[str, str]:
    """The three trigger statements for one synced table, keyed by trigger name."""
    key_columns = sync_key(table, (name for name, position in sorted(columns, key=lambda c: c[1]) if position))
    if not key_columns:
        # Every row would be recorded under the same empty key (I32 refuses such a table).
        raise ValueError(f"{table}: no primary key and no SYNC_KEYS entry, its changes cannot be keyed")
    local = LOCAL_COLUMNS.get(table, frozenset())
    statements = {}
    for short, kind, event, alias in _KINDS:
        if event == "UPDATE" and local:
            watched = ", ".join(_quote(name) for name, _ in columns if name not in local)
            event = f"UPDATE OF {watched}"
        body = _record(table, _key_sql(alias, key_columns), kind)
        if kind == "U":
            # A changed key reads as the old key deleted and the new one written.
            old_key, new_key = _key_sql("OLD", key_columns), _key_sql("NEW", key_columns)
            body = _record(table, old_key, "D", f"{old_key} IS NOT {new_key}") + " " + body
        name = trigger_name(short, table)
        statements[name] = (
            f"CREATE TRIGGER {_quote(name)} AFTER {event} ON {_quote(table)} "
            f"WHEN {_RECORDING} BEGIN {body} END"
        )
    return statements


def expected_triggers(conn: Connection) -> dict[str, str]:
    """Every change-capture trigger this database should carry, by name."""
    present = {row[0] for row in conn.exec_driver_sql("SELECT name FROM sqlite_master WHERE type = 'table'")}
    expected: dict[str, str] = {}
    for table in sorted(SYNCED_TABLES & present):
        expected.update(trigger_sql(table, _table_columns(conn, table)))
    return expected


def _installed_triggers(conn: Connection) -> dict[str, str]:
    rows = conn.exec_driver_sql("SELECT name, sql FROM sqlite_master WHERE type = 'trigger'").all()
    return {name: sql for name, sql in rows if _OUR_TRIGGER.match(name)}


def _installed_view(conn: Connection) -> str | None:
    row = conn.exec_driver_sql(
        "SELECT sql FROM sqlite_master WHERE type = 'view' AND name = ?", (CLOCK_VIEW,)
    ).first()
    return row[0] if row else None


def trigger_problems(conn: Connection) -> list[str]:
    """What differs from the expected objects: missing, stale or orphaned triggers, the clock view."""
    expected, installed = expected_triggers(conn), _installed_triggers(conn)
    problems = [f"{name}: 缺失" for name in sorted(expected.keys() - installed.keys())]
    problems += [f"{name}: 定义与当前表结构不符" for name in sorted(expected.keys() & installed.keys())
                 if expected[name] != installed[name]]
    problems += [f"{name}: 所在表不再同步" for name in sorted(installed.keys() - expected.keys())]
    view = _installed_view(conn)
    if view != CLOCK_VIEW_SQL:
        problems.append(f"{CLOCK_VIEW}: {'缺失' if view is None else '定义与代码不符'}")
    return problems


def _ensure_state_row(conn: Connection) -> None:
    conn.exec_driver_sql("INSERT OR IGNORE INTO sync_state (id, node_id, capture) VALUES (1, '', 0)")


def install_change_triggers(conn: Connection) -> int:
    """Bring the clock view and the triggers in line with the current tables.

    Returns how many triggers were (re)created. The first statement is a write, so
    the transaction holds the write lock before it reads the schema.
    """
    _ensure_state_row(conn)
    if _installed_view(conn) != CLOCK_VIEW_SQL:
        conn.exec_driver_sql(f"DROP VIEW IF EXISTS {_quote(CLOCK_VIEW)}")
        conn.exec_driver_sql(CLOCK_VIEW_SQL)
    expected, installed = expected_triggers(conn), _installed_triggers(conn)
    for name in sorted(installed.keys() - expected.keys()):
        conn.exec_driver_sql(f"DROP TRIGGER {_quote(name)}")
    created = 0
    for name, statement in expected.items():
        if installed.get(name) == statement:
            continue
        conn.exec_driver_sql(f"DROP TRIGGER IF EXISTS {_quote(name)}")
        conn.exec_driver_sql(statement)
        created += 1
    return created


async def reinstall_change_triggers(engine: AsyncEngine) -> None:
    """Run after migrations: they may have rebuilt a table or added a column."""
    if engine.dialect.name != "sqlite":
        return
    async with engine.begin() as conn:
        await conn.run_sync(install_change_triggers)


def set_node(conn: Connection, node_id: str, *, capture: bool) -> None:
    """Record this node's id (the clock suffix) and whether its writes are captured."""
    _ensure_state_row(conn)
    conn.exec_driver_sql(
        "UPDATE sync_state SET node_id = ?, capture = ? WHERE id = 1", (node_id, 1 if capture else 0)
    )


def suppress(conn: Connection, holder: str) -> None:
    """Make this transaction's later writes invisible to the change log. Release before commit."""
    conn.exec_driver_sql("INSERT OR REPLACE INTO sync_suppress (holder) VALUES (?)", (holder,))


def release(conn: Connection, holder: str) -> None:
    conn.exec_driver_sql("DELETE FROM sync_suppress WHERE holder = ?", (holder,))


@contextmanager
def suppressed(conn: Connection, holder: str = APPLIER_HOLDER) -> Iterator[None]:
    """Writes in this block are not changes; open it first thing in a transaction.

    The marker is released when the block ends normally; on an error it is left to
    the transaction's rollback, which the caller must perform (``engine.begin()``
    does). Either way it is never committed.
    """
    suppress(conn, holder)
    yield
    release(conn, holder)
