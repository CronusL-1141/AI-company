"""Change capture: SQLite triggers that log every write to a synced table.

Triggers rather than repository hooks, because not every write goes through the
repository: startup migrations, maintenance scripts and raw SQL write tables
directly, and a trigger sees them all, deletes included.

Each synced table gets three triggers (``sync_i_<t>``, ``sync_u_<t>``,
``sync_d_<t>``) appending ``(table_name, pk, kind, hlc)`` to ``sync_changes``; the
clock value comes from the view ``sync_next_hlc``, one definition for all of them.
They record only while

* ``sync_state.capture`` holds the table's face (``CAPTURE_BITS``): a node that
  syncs that face. A standalone node keeps the triggers but records nothing, so its
  database grows by nothing and holds exactly what it held before. Edges and the
  center capture the shared face; the session face is captured once it travels; and
* ``sync_suppress`` is empty. A writer whose writes are not changes to ship (a
  sync applier, a startup migration that runs in one transaction) inserts its row
  as the first write of its own transaction and deletes it before committing
  (``suppressed``). SQLite has one writer at a time, so no other connection ever
  sees the row, and a process killed mid-transaction rolls it back. A committed
  marker would silence every other writer of the database while it stands, and a
  killed holder would leave it standing: work that cannot run in one transaction is
  not suppressed at all. Recording too much is harmless, the replay is idempotent;
  recording too little loses data.

A session-face trigger also records while ``sync_guard`` holds a row, whatever the
capture: the center carrying out another node's operation sets it for that
operation's transaction, to check that it wrote no session row it does not own.

A trigger must never fail the write it observes: every value it reads is
coalesced, so a missing ``sync_state`` row means "do not record", not an error.

Two more kinds ride along while a node syncs. ``sync_f_<t>`` keeps a clock per
field of the mergeable shared tables (``FIELD_CLOCK_TABLES``) in ``sync_field_clock``:
the center orders a replayed write of a field against the last write it saw.
``sync_t_<t>`` remembers the deleted ids of natural-key tables (``TOMBSTONE_TABLES``)
in ``sync_tombstones``, whatever wrote the deletion, so a re-created project or team
takes a new generation of its id.

Triggers are reinstalled after every startup migration (``install_change_triggers``):
a migration may rebuild a table, which drops its triggers, or add a column, which
changes the column list an update trigger watches. Installation compares the stored
SQL and only touches objects that differ.

The node's clock is the later of the last row of ``sync_changes`` and the floor in
``sync_state.hlc``, which records clock values issued or observed outside the log
(``next_hlc``, ``observe_hlc``); pruning the log raises the floor first.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine

from aiteam.sync import hlc as clock
from aiteam.sync.hlc import tick_sql
from aiteam.sync.rows import quote
from aiteam.sync.tables import (
    CONFIG_FIELDS,
    FIELD_CLOCK_TABLES,
    LOCAL_COLUMNS,
    ORIGIN_COLUMN,
    SHARED_TABLES,
    SYNCED_TABLES,
    TOMBSTONE_TABLES,
    sync_key,
)
from aiteam.types import SyncFace

MIGRATION_HOLDER = "migration"
APPLIER_HOLDER = "applier"
GUARD_HOLDER = "foreign-operation"

# Which faces a node captures: sync_state.capture is a sum of these bits.
CAPTURE_BITS = {SyncFace.SHARED: 1, SyncFace.SESSION: 2}

CLOCK_VIEW = "sync_next_hlc"
_KINDS = (("i", "I", "INSERT", "NEW"), ("u", "U", "UPDATE", "NEW"), ("d", "D", "DELETE", "OLD"))
_OUR_TRIGGER = re.compile(r"^sync_[iudft]_")

_PHYSICAL_MS = "CAST((julianday('now') - 2440587.5) * 86400000 AS INTEGER)"
# The clock's last value: the later of the last change and the floor.
LAST_HLC_SQL = (
    "(SELECT nullif(max(h), '') FROM (SELECT (SELECT hlc FROM sync_changes ORDER BY seq DESC LIMIT 1) AS h "
    "UNION ALL SELECT hlc FROM sync_state WHERE id = 1))"
)
_NODE = "(SELECT node_id FROM sync_state WHERE id = 1)"
_CAPTURE = "coalesce((SELECT capture FROM sync_state WHERE id = 1), 0)"
_NOT_SUPPRESSED = "NOT EXISTS (SELECT 1 FROM sync_suppress)"
_GUARDED = "EXISTS (SELECT 1 FROM sync_guard)"
CLOCK_VIEW_SQL = f'CREATE VIEW "{CLOCK_VIEW}" AS {tick_sql(LAST_HLC_SQL, _NODE, _PHYSICAL_MS)}'


def _face(table: str) -> SyncFace:
    return SyncFace.SHARED if table in SHARED_TABLES else SyncFace.SESSION


def _recording(table: str) -> str:
    face = _face(table)
    captured = f"({_CAPTURE} & {CAPTURE_BITS[face]})"
    if face is SyncFace.SESSION:
        captured = f"({captured} OR {_GUARDED})"
    return f"{captured} AND {_NOT_SUPPRESSED}"


def trigger_name(kind: str, table: str) -> str:
    return f"sync_{kind}_{table}"


def _key_sql(alias: str, columns: tuple[str, ...]) -> str:
    if len(columns) == 1:
        return f"coalesce(CAST({alias}.{quote(columns[0])} AS TEXT), '')"
    return "json_array(" + ", ".join(f"{alias}.{quote(c)}" for c in columns) + ")"


def _record(table: str, key_sql: str, kind: str, condition: str = "") -> str:
    where = f" WHERE {condition}" if condition else ""
    return (
        f"INSERT INTO sync_changes (table_name, pk, kind, hlc) "
        f"SELECT '{table}', {key_sql}, '{kind}', hlc FROM {quote(CLOCK_VIEW)}{where};"
    )


def _table_columns(conn: Connection, table: str) -> list[tuple[str, int]]:
    """(name, primary-key position) per column, in table order."""
    rows = conn.exec_driver_sql(f"PRAGMA table_info({quote(table)})").all()
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
            watched = ", ".join(quote(name) for name, _ in columns if name not in local)
            event = f"UPDATE OF {watched}"
        body = _record(table, _key_sql(alias, key_columns), kind)
        if kind == "U":
            # A changed key reads as the old key deleted and the new one written.
            old_key, new_key = _key_sql("OLD", key_columns), _key_sql("NEW", key_columns)
            body = _record(table, old_key, "D", f"{old_key} IS NOT {new_key}") + " " + body
        name = trigger_name(short, table)
        statements[name] = (
            f"CREATE TRIGGER {quote(name)} AFTER {event} ON {quote(table)} "
            f"WHEN {_recording(table)} BEGIN {body} END"
        )
    if table in FIELD_CLOCK_TABLES:
        statements[trigger_name("f", table)] = _field_clock_sql(table, columns, key_columns)
    if table in TOMBSTONE_TABLES:
        statements[trigger_name("t", table)] = _tombstone_sql(table, key_columns)
    return statements


def _field_clock_sql(table: str, columns: list[tuple[str, int]], key_columns: tuple[str, ...]) -> str:
    """One clock per field an update changed, stamped with the node that wrote it.

    Fields kept inside ``config`` (CONFIG_FIELDS) get clocks of their own; the JSON is
    read only when valid, so a malformed value never fails the write.
    """
    skip = set(key_columns) | {ORIGIN_COLUMN} | LOCAL_COLUMNS.get(table, frozenset())
    key = _key_sql("NEW", key_columns)
    changed = {name: f"OLD.{quote(name)} IS NOT NEW.{quote(name)}" for name, _ in columns if name not in skip}
    present = {name for name, _ in columns}
    for name, path in CONFIG_FIELDS.get(table, {}).items():
        if "config" in present:
            json_path = "$." + ".".join(path)
            old, new = (f"iif(json_valid({side}.\"config\"), json_extract({side}.\"config\", '{json_path}'), NULL)"
                        for side in ("OLD", "NEW"))
            changed[name] = f"{old} IS NOT {new}"
    body = " ".join(
        f"INSERT INTO sync_field_clock (table_name, pk, field, hlc, node) "
        f"SELECT '{table}', {key}, '{name}', hlc, coalesce({_NODE}, '') FROM {quote(CLOCK_VIEW)} "
        f"WHERE {condition} "
        f"ON CONFLICT (table_name, pk, field) DO UPDATE SET hlc = excluded.hlc, node = excluded.node;"
        for name, condition in changed.items()
    )
    name = trigger_name("f", table)
    return f"CREATE TRIGGER {quote(name)} AFTER UPDATE ON {quote(table)} WHEN {_recording(table)} BEGIN {body} END"


def _tombstone_sql(table: str, key_columns: tuple[str, ...]) -> str:
    """Remember a deleted id while the node syncs, whoever deletes (the applier too)."""
    name = trigger_name("t", table)
    return (
        f"CREATE TRIGGER {quote(name)} AFTER DELETE ON {quote(table)} WHEN {_CAPTURE} != 0 BEGIN "
        f"INSERT OR IGNORE INTO sync_tombstones (table_name, pk, hlc) "
        f"SELECT '{table}', {_key_sql('OLD', key_columns)}, hlc FROM {quote(CLOCK_VIEW)}; END"
    )


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
        conn.exec_driver_sql(f"DROP VIEW IF EXISTS {quote(CLOCK_VIEW)}")
        conn.exec_driver_sql(CLOCK_VIEW_SQL)
    expected, installed = expected_triggers(conn), _installed_triggers(conn)
    for name in sorted(installed.keys() - expected.keys()):
        conn.exec_driver_sql(f"DROP TRIGGER {quote(name)}")
    created = 0
    for name, statement in expected.items():
        if installed.get(name) == statement:
            continue
        conn.exec_driver_sql(f"DROP TRIGGER IF EXISTS {quote(name)}")
        conn.exec_driver_sql(statement)
        created += 1
    return created


async def reinstall_change_triggers(engine: AsyncEngine) -> None:
    """Run after migrations: they may have rebuilt a table or added a column."""
    if engine.dialect.name != "sqlite":
        return
    async with engine.begin() as conn:
        await conn.run_sync(install_change_triggers)


def set_node(conn: Connection, node_id: str, faces: frozenset[SyncFace]) -> None:
    """Record this node's id (the clock suffix) and which faces its triggers capture."""
    _ensure_state_row(conn)
    conn.exec_driver_sql(
        "UPDATE sync_state SET node_id = ?, capture = ? WHERE id = 1",
        (node_id, sum(CAPTURE_BITS[face] for face in faces)),
    )


def next_hlc(conn: Connection) -> str:
    """Issue a clock value outside the change log (an operation's), and keep it as the floor.

    Run inside a write transaction: the read and the floor update must not interleave
    with another writer's.
    """
    _ensure_state_row(conn)
    value = conn.exec_driver_sql(f"SELECT hlc FROM {quote(CLOCK_VIEW)}").scalar_one()
    conn.exec_driver_sql("UPDATE sync_state SET hlc = ? WHERE id = 1", (value,))
    return value


def latest_seq(conn: Connection) -> int:
    """The last change this node recorded (0 for none): where its change sequence stands."""
    return conn.exec_driver_sql("SELECT coalesce(max(seq), 0) FROM sync_changes").scalar_one()


def raise_floor(conn: Connection) -> None:
    """Keep the clock where it is before change-log rows are removed (pruned or settled)."""
    _ensure_state_row(conn)
    conn.exec_driver_sql(f"UPDATE sync_state SET hlc = coalesce({LAST_HLC_SQL}, '') WHERE id = 1")


def observe_hlc(conn: Connection, remote: str) -> str:
    """Move this node's clock past a value received from a peer; raises HlcDriftError.

    Every change recorded afterwards orders after the peer's write, so a later local
    write of a field wins over the replayed one, as it should.
    """
    _ensure_state_row(conn)
    last_text, node = conn.exec_driver_sql(f"SELECT {LAST_HLC_SQL}, {_NODE}").one()
    last = clock.parse(last_text) if last_text else None
    value = clock.encode(clock.observe(last, clock.parse(remote), node or "", clock.now_ms()))
    conn.exec_driver_sql("UPDATE sync_state SET hlc = ? WHERE id = 1", (value,))
    return value


def guard(conn: Connection, holder: str = GUARD_HOLDER) -> None:
    """Record this transaction's session writes even where that face is not captured.

    For the center carrying out another node's operation: what the operation wrote on
    the session face is checked before the transaction commits (aiteam.sync.effects).
    Open it first thing in the transaction and release it before committing, like
    ``suppress``; a rollback takes it down.
    """
    conn.exec_driver_sql("INSERT OR REPLACE INTO sync_guard (holder) VALUES (?)", (holder,))


def unguard(conn: Connection, holder: str = GUARD_HOLDER) -> None:
    conn.exec_driver_sql("DELETE FROM sync_guard WHERE holder = ?", (holder,))


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
