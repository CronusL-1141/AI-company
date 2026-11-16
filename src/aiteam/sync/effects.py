"""One writer per origin: the center hands an operation's session writes back to its node.

The session face of a node (its events, agents, teams, activities) is written by that
node alone and goes up to the center as it is (aiteam.sync.intake). An operation an
edge forwards or replays runs on the center, and its route may write the session
face: every write emits an event, a task assignment sets the assignee's current
task. Written on the center, those rows would have two writers, and the edge would
never see its own events. So while the center carries out another node's operation
(aiteam.sync.context.foreign_effects):

* a new event is not stored: it travels back in the verdict (``event``);
* a session row the operation's node owns is not changed: the change travels back
  (``row_update``); one the center owns is changed here as always; one a third node
  owns is left alone and logged (cross-machine session actions are not routed yet);
* the edge writes them when it settles the operation (``apply``), as its own writes:
  they reach the center again with the edge's session face. Events always; row
  updates only for an operation the edge did not stage, since staging made them here.

``guard_problems`` is the check that nothing else slipped through: the center's
transaction for the operation records its session writes (aiteam.sync.changelog
``guard``), and a write of a row the center does not own, or a deletion, refuses the
operation (503: it stays queued, the edge's status turns red).

A shared-face route that comes to write another kind of session row needs a hand-back
here and in the repository method that writes it (as ``create_event`` and
``update_agent`` have): the guard only stops the operation, it does not route the write.
tests/integration/test_sync_session_face.py drives every offline-capable shared write of
an edge's session through the center and ends with the one-writer comparison, so such
a route is found there before it ships.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.engine import Connection

from aiteam.storage.models import Base
from aiteam.sync.changelog import raise_floor
from aiteam.sync.rows import Rows, quote, stored_form
from aiteam.sync.tables import ORIGIN_COLUMN, SESSION_TABLES, SESSION_TABLES_SQL
from aiteam.types import SyncEffect, SyncEffectKind

logger = logging.getLogger(__name__)


def new_row(table: str, values: dict[str, Any], origin: str) -> SyncEffect:
    """A new session row of ``origin`` (an event), in the form the database stores it."""
    row = stored_form(Base.metadata.tables[table], {**values, ORIGIN_COLUMN: origin})
    key = Base.metadata.tables[table].primary_key.columns.keys()[0]
    return SyncEffect(kind=SyncEffectKind.EVENT, table_name=table, pk=str(row[key]), row=row)


def changed_fields(table: str, pk: str, fields: dict[str, Any]) -> SyncEffect:
    return SyncEffect(kind=SyncEffectKind.ROW_UPDATE, table_name=table, pk=pk,
                      fields=stored_form(Base.metadata.tables[table], fields))


def apply(conn: Connection, effects: list[SyncEffect], *, staged: bool) -> None:
    """Write an operation's session effects here, in the settling transaction (not suppressed)."""
    rows = Rows(conn, SESSION_TABLES)
    for effect in effects:
        if effect.table_name not in SESSION_TABLES:
            logger.warning("Multi-host sync: ignored an effect on %s (not a session table)", effect.table_name)
            continue
        if effect.kind is SyncEffectKind.EVENT and effect.row is not None:
            columns = [c for c in rows.shape(effect.table_name).columns if c in effect.row]
            conn.exec_driver_sql(
                f"INSERT OR IGNORE INTO {quote(effect.table_name)} ({', '.join(quote(c) for c in columns)}) "
                f"VALUES ({', '.join('?' for _ in columns)})", tuple(effect.row[c] for c in columns))
        elif effect.kind is SyncEffectKind.ROW_UPDATE and effect.fields and not staged:
            shape = rows.shape(effect.table_name)
            assignments = ", ".join(f"{quote(c)} = ?" for c in effect.fields)
            conn.exec_driver_sql(
                f"UPDATE {quote(effect.table_name)} SET {assignments} WHERE {shape.where_key()}",
                (*effect.fields.values(), *shape.key_values(effect.pk)))


def guard_problems(conn: Connection, after: int, center: str) -> list[str]:
    """Session writes the center made for another node's operation that it may not make.

    Reads the rows the guarded transaction touched after ``after``: one still there must
    be the center's own; one gone was deleted, which an operation of another node never
    does to the session face (no row's owner can be shown, and the owner deletes its own
    rows). When nothing is wrong, removes those records (the center does not capture
    its session face).
    """
    touched = conn.exec_driver_sql(
        f"SELECT DISTINCT table_name, pk FROM sync_changes WHERE seq > ? AND table_name IN ({SESSION_TABLES_SQL})",
        (after,)).all()
    rows = Rows(conn, SESSION_TABLES)
    problems = []
    for table, pk in touched:
        current = rows.read(table, pk)
        if current is None:
            problems.append(f"{table}({pk}) 删除")
        elif (owner := current.get(ORIGIN_COLUMN) or "") != center:
            problems.append(f"{table}({pk}) 写入，行属于 {owner or '基线（空来源）'}")
    if touched and not problems:
        raise_floor(conn)
        conn.exec_driver_sql(f"DELETE FROM sync_changes WHERE seq > ? AND table_name IN ({SESSION_TABLES_SQL})",
                             (after,))
    return problems
