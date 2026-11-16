"""The center takes in its edges' session faces: one writer per origin, the union kept.

An edge sends the session rows it changed, batch by batch in its change sequence
(aiteam.sync.upload). The center keeps where each edge stands (``sync_intake``) and
takes a batch only when

* it continues where the edge stands here (``after`` not past the cursor; a batch
  that starts further on means the center lost some of it: ``gap``), and
* it was read no earlier than the last batch taken (``snapshot`` not behind): a
  batch sent before, delayed, would otherwise bring back older rows (``stale``).

Rows are rows of their origin's sessions, and only that node writes them:

* a row must claim the batch's origin, or the empty origin of the baseline (rows
  from before the cut-over, which belong to the edge whose database was the
  baseline: the first edge the center ever heard from); anything else is refused;
* a row the center holds with another owner is skipped and stays as it is (written
  on two machines: the owner wins, aiteam.types.SyncSessionAck);
* a unique value held by another row of the same owner moves to the incoming row
  (the edge re-created it under another id); one held by another owner skips it;
* columns numbered per database get the center's next number, the edge's number
  is kept beside it (SESSION_RENUMBERED).

Taking a batch in holds the write lock for a few milliseconds; afterwards the
center waits several times as long before answering (``INTAKE_DUTY``), so an edge
catching up after a day offline never holds up the center's own hook writes.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy.engine import Connection

from aiteam.clock import utc_now
from aiteam.storage.connection import get_engine
from aiteam.storage.partition import driver_sql, edge_origins
from aiteam.sync import context as sync_context
from aiteam.sync.changelog import suppressed
from aiteam.sync.protocol import INTAKE_DUTY, timing
from aiteam.sync.rows import Rows, quote
from aiteam.sync.tables import ORIGIN_COLUMN, SESSION_RENUMBERED, SESSION_TABLES
from aiteam.types import SyncIntakeOutcome, SyncIntakeStatus, SyncSessionAck, SyncSessionBatch, SyncSessionKeys

logger = logging.getLogger(__name__)

_APPLIED, _SKIPPED, _REFUSED = "applied", "skipped", "refused"


class _SessionRows(Rows):
    """The center's session rows, written for one origin."""

    def __init__(self, conn: Connection, origin: str, owns_baseline: bool) -> None:
        super().__init__(conn, SESSION_TABLES)
        self.owners = set(_owners(origin, owns_baseline))
        self.origin = origin

    def take(self, table: str, pk: str, row: dict[str, Any] | None) -> str:
        """Apply one row of the batch (None: its deletion); the verdict for the counts."""
        if table not in SESSION_TABLES:
            return _REFUSED
        claimed = self._claim(row)
        if claimed is not None:
            return claimed
        current = self.read(table, pk)
        if current is not None and not self._owned(current):
            return _SKIPPED
        if row is None:
            if current is not None:
                self.delete(table, pk)
            return _APPLIED
        incoming = self._numbered_here(table, row, current)
        if not self._free_unique_values(table, pk, incoming):
            return _SKIPPED
        self.upsert(table, incoming, [c for c in self.shape(table).columns if c in incoming])
        return _APPLIED

    def _claim(self, row: dict[str, Any] | None) -> str | None:
        """The verdict on the origin the row claims, when it is not to be taken."""
        if row is None:
            return None
        claimed = row.get(ORIGIN_COLUMN) or ""
        if claimed not in (self.origin, ""):
            return _REFUSED
        return None if claimed in self.owners else _SKIPPED

    def _owned(self, row: dict[str, Any]) -> bool:
        return (row.get(ORIGIN_COLUMN) or "") in self.owners

    def _numbered_here(self, table: str, row: dict[str, Any],
                       current: dict[str, Any] | None) -> dict[str, Any]:
        """The row with this database's number in a column numbered per database
        (SESSION_RENUMBERED), the edge's number kept beside it. Done before any unique
        value is looked at: the edge's number means nothing among the center's."""
        renumbered = SESSION_RENUMBERED.get(table)
        if not renumbered:
            return row
        local, kept = renumbered
        number = current[local] if current is not None else self._next_number(table, local)
        return {**row, kept: row.get(local), local: number}

    def _free_unique_values(self, table: str, pk: str, row: dict[str, Any]) -> bool:
        """Remove rows of the same owner holding the row's other unique values; False
        when another owner holds one (the incoming row is then not taken)."""
        shape = self.shape(table)
        key = shape.key_values(pk)
        for unique in shape.unique_values_of(row):
            where = " AND ".join(f"{quote(c)} IS ?" for c in unique) + f" AND NOT ({shape.where_key()})"
            params = (*[row[c] for c in unique], *key)
            holders = self.conn.exec_driver_sql(
                f"SELECT {quote(ORIGIN_COLUMN)} FROM {quote(table)} WHERE {where}", params).all()
            if any((origin or "") not in self.owners for (origin,) in holders):
                return False
            if holders:
                self.conn.exec_driver_sql(f"DELETE FROM {quote(table)} WHERE {where}", params)
        return True

    def _next_number(self, table: str, column: str) -> int:
        return int(self.conn.exec_driver_sql(
            f"SELECT coalesce(max({quote(column)}), 0) + 1 FROM {quote(table)}").scalar_one())


def _owners(origin: str, owns_baseline: bool) -> tuple[str, ...]:
    """The origins whose rows here only ``origin`` writes."""
    return edge_origins(origin) if owns_baseline else (origin,)


def _held_keys(conn: Connection, origin: str, owns_baseline: bool, table: str, after: int,
               limit: int) -> SyncSessionKeys:
    rows = Rows(conn, SESSION_TABLES)
    owned, owners = driver_sql(_owners(origin, owns_baseline))
    key = ", ".join(quote(c) for c in rows.shape(table).key)
    found = conn.exec_driver_sql(
        f"SELECT rowid AS _sync_rowid, {key} FROM {quote(table)} WHERE {owned} AND rowid > ? "
        "ORDER BY rowid LIMIT ?", (*owners, after, limit)).mappings().all()
    return SyncSessionKeys(keys=[rows.key_of(table, row) for row in found],
                           after=found[-1]["_sync_rowid"] if found else after, done=len(found) < limit)


def _status(row: Any) -> SyncIntakeStatus:
    return SyncIntakeStatus(node=row[0], cursor=row[1], baseline_owner=bool(row[3]), applied=row[4],
                            skipped_foreign=row[5], refused=row[6], last_batch_at=row[7])


_COLUMNS = "node, cursor, snapshot, baseline_owner, applied, skipped_foreign, refused, last_batch_at"


def take_in(conn: Connection, batch: SyncSessionBatch) -> SyncSessionAck:
    """Apply one batch inside the caller's write transaction (suppressed by the caller)."""
    now = utc_now().isoformat()
    # The first edge the center ever hears from owns the baseline's empty-origin rows.
    conn.exec_driver_sql(
        "INSERT OR IGNORE INTO sync_intake (node, cursor, snapshot, baseline_owner, applied, skipped_foreign, "
        "refused, updated_at) SELECT ?, 0, 0, NOT EXISTS (SELECT 1 FROM sync_intake), 0, 0, 0, ?",
        (batch.origin_node, now))
    cursor, snapshot, owns_baseline = conn.exec_driver_sql(
        "SELECT cursor, snapshot, baseline_owner FROM sync_intake WHERE node = ?", (batch.origin_node,)).one()
    if batch.after > cursor:
        return SyncSessionAck(cursor=cursor, outcome=SyncIntakeOutcome.GAP)
    if batch.snapshot < snapshot:
        return SyncSessionAck(cursor=cursor, outcome=SyncIntakeOutcome.STALE)
    rows = _SessionRows(conn, batch.origin_node, bool(owns_baseline))
    counts = {_APPLIED: 0, _SKIPPED: 0, _REFUSED: 0}
    for change in batch.changes:
        verdict = rows.take(change.table_name, change.pk, change.row)
        counts[verdict] += 1
        if verdict == _REFUSED:
            logger.warning("Multi-host sync: refused %s %s from %s (claims origin %r)", change.table_name,
                           change.pk, batch.origin_node, (change.row or {}).get(ORIGIN_COLUMN))
    cursor = max(cursor, batch.upto)
    conn.exec_driver_sql(
        "UPDATE sync_intake SET cursor = ?, snapshot = ?, applied = applied + ?, skipped_foreign = "
        "skipped_foreign + ?, refused = refused + ?, last_batch_at = ?, updated_at = ? WHERE node = ?",
        (cursor, batch.snapshot, counts[_APPLIED], counts[_SKIPPED], counts[_REFUSED], now, now,
         batch.origin_node))
    return SyncSessionAck(cursor=cursor, outcome=SyncIntakeOutcome.APPLIED, applied=counts[_APPLIED],
                          skipped_foreign=counts[_SKIPPED], refused=counts[_REFUSED])


class Intake:
    """The center's intake of its edges' session faces."""

    def __init__(self, db_url: str, node_id: str) -> None:
        self._db_url = db_url
        self.node_id = node_id
        self._locks: dict[str, asyncio.Lock] = {}

    @asynccontextmanager
    async def _held(self, node: str) -> AsyncIterator[None]:
        lock = self._locks.setdefault(node, asyncio.Lock())
        async with lock:
            yield

    async def load(self) -> None:
        """Learn which edge owns the baseline (startup)."""
        sync_context.set_baseline_owner(await self.baseline_owner())

    async def apply(self, batch: SyncSessionBatch, *, pace: bool = True) -> SyncSessionAck:
        """Take one batch in, then yield the write lock for a while (``INTAKE_DUTY``).

        ``pace`` False for the tail a forward carries: one small batch, a caller waiting.
        """
        if batch.origin_node == self.node_id:
            raise ValueError("会话面批次来自中心自己")
        loop = asyncio.get_running_loop()
        async with self._held(batch.origin_node):
            started = loop.time()
            async with get_engine(self._db_url).begin() as conn:
                ack = await conn.run_sync(_take_in_suppressed, batch)
            took = loop.time() - started
        if ack.outcome is SyncIntakeOutcome.APPLIED and not sync_context.baseline_owner():
            sync_context.set_baseline_owner(await self.baseline_owner())
        duty = timing("intake_duty", INTAKE_DUTY)
        if pace and batch.changes and duty < 1:
            await asyncio.sleep(took * (1 - duty) / duty)
        return ack

    async def cursor(self, node: str) -> int | None:
        async with get_engine(self._db_url).connect() as conn:
            row = (await conn.exec_driver_sql("SELECT cursor FROM sync_intake WHERE node = ?", (node,))).first()
        return row[0] if row is not None else None

    async def baseline_owner(self) -> str:
        async with get_engine(self._db_url).connect() as conn:
            row = (await conn.exec_driver_sql("SELECT node FROM sync_intake WHERE baseline_owner")).first()
        return row[0] if row is not None else ""

    async def held_keys(self, node: str, table: str, after: int, limit: int) -> SyncSessionKeys:
        """One page of the keys of ``node``'s rows in a session table here (with the
        baseline's, when ``node`` owns the baseline), for ``resend-session``."""
        if table not in SESSION_TABLES:
            raise ValueError(f"不是会话面的表：{table}")
        owns_baseline = await self.baseline_owner() == node
        async with get_engine(self._db_url).connect() as conn:
            return await conn.run_sync(_held_keys, node, owns_baseline, table, after, limit)

    async def statuses(self) -> list[SyncIntakeStatus]:
        async with get_engine(self._db_url).connect() as conn:
            rows = (await conn.exec_driver_sql(f"SELECT {_COLUMNS} FROM sync_intake ORDER BY node")).all()
        return [_status(row) for row in rows]


def _take_in_suppressed(conn: Connection, batch: SyncSessionBatch) -> SyncSessionAck:
    with suppressed(conn):
        return take_in(conn, batch)
