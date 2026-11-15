"""What the center serves its edges to read: the handshake, its changes, rows by key.

* ``hello``: who this node is and what it syncs under. Given the edge's cursor and
  the clock it recorded there, it also says whether that change is still where the
  edge saw it: a center restored from an old backup reuses sequence numbers for
  other changes, which a bare "sequence went back" check misses once it has written
  past the cursor again.
* ``Feed.changes``: rows changed after a cursor, each under its latest change.
* ``Feed.rows`` / ``Feed.snapshot``: current rows by key, and whole tables page by
  page for an edge that has to rebuild its replica.
* ``Feed.prune``: changes older than the retention go, in chunks.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.engine import Connection

from aiteam import __version__
from aiteam.clock import utc_now
from aiteam.storage.connection import get_engine
from aiteam.sync import hlc as clock
from aiteam.sync.changelog import raise_floor
from aiteam.sync.protocol import SYNC_PROTOCOL, TOMBSTONE_RETENTION_DAYS, schema_version
from aiteam.sync.replica import Replica
from aiteam.sync.tables import SHARED_TABLES, SHARED_TABLES_SQL
from aiteam.types import NodeRole, SyncChange, SyncChangesPage, SyncHello

_PRUNE_CHUNK = 5000


def _hlc_at(conn: Connection, seq: int) -> str | None:
    return conn.exec_driver_sql("SELECT hlc FROM sync_changes WHERE seq = ?", (seq,)).scalar()


def _cursor_known(conn: Connection, cursor: int | None, cursor_hlc: str) -> bool | None:
    if not cursor or cursor <= 0 or not cursor_hlc:
        return None
    pruned = conn.exec_driver_sql("SELECT pruned_seq FROM sync_state WHERE id = 1").scalar() or 0
    if cursor <= pruned:
        return None
    return _hlc_at(conn, cursor) == cursor_hlc


async def hello(db_url: str, node_id: str, role: NodeRole, *, cursor: int | None = None,
                cursor_hlc: str = "") -> SyncHello:
    def read(conn: Connection) -> SyncHello:
        user_version = conn.exec_driver_sql("PRAGMA user_version").scalar_one()
        high = conn.exec_driver_sql("SELECT coalesce(max(seq), 0) FROM sync_changes").scalar_one()
        baseline = conn.exec_driver_sql("SELECT baseline_seq FROM sync_state WHERE id = 1").scalar() or 0
        return SyncHello(
            node_id=node_id, role=role, sync_protocol=SYNC_PROTOCOL, user_version=user_version,
            schema_version=schema_version(), api_version=__version__, baseline_seq=baseline, max_seq=high,
            cursor_known=_cursor_known(conn, cursor, cursor_hlc),
        )

    async with get_engine(db_url).connect() as conn:
        return await conn.run_sync(read)


class Feed:
    """The center's change log, served to its edges."""

    def __init__(self, db_url: str) -> None:
        self._db_url = db_url

    async def _read(self, read: Any) -> Any:
        async with get_engine(self._db_url).connect() as conn:
            return await conn.run_sync(read)

    async def changes(self, after: int, limit: int) -> SyncChangesPage:
        """Rows changed after ``after``, each once under its latest change, oldest first.

        A row is read as it is now, possibly newer than the batch: applying a newer
        state early is harmless, the next batch brings the same one again.
        """
        def read(conn: Connection) -> SyncChangesPage:
            high = conn.exec_driver_sql("SELECT coalesce(max(seq), 0) FROM sync_changes").scalar_one()
            pruned = conn.exec_driver_sql("SELECT pruned_seq FROM sync_state WHERE id = 1").scalar() or 0
            if after < pruned:
                return SyncChangesPage(max_seq=high, rebuild=True)
            rows = conn.exec_driver_sql(
                f"SELECT seq, table_name, pk FROM sync_changes WHERE seq > ? AND seq <= ? "
                f"AND table_name IN ({SHARED_TABLES_SQL}) ORDER BY seq LIMIT ?", (after, high, limit)).all()
            latest = {(table, pk): seq for seq, table, pk in rows}
            replica = Replica(conn)
            changes = [SyncChange(seq=seq, table_name=table, pk=pk, row=replica.read(table, pk))
                       for (table, pk), seq in sorted(latest.items(), key=lambda item: item[1])]
            upto = rows[-1][0] if len(rows) == limit else max(high, after)
            return SyncChangesPage(changes=changes, upto=upto, upto_hlc=_hlc_at(conn, upto) or "", max_seq=high)

        return await self._read(read)

    async def snapshot(self, table: str, after: int, limit: int) -> dict[str, Any]:
        """One page of a shared table in storage order, for an edge rebuilding its replica.

        ``max_seq`` is read before the rows: changes after it reach the edge again.
        """
        if table not in SHARED_TABLES:
            raise ValueError(f"{table} 不是共享面的表")

        def read(conn: Connection) -> dict[str, Any]:
            max_seq = conn.exec_driver_sql("SELECT coalesce(max(seq), 0) FROM sync_changes").scalar_one()
            result = conn.exec_driver_sql(
                f'SELECT rowid AS _sync_rowid, * FROM "{table}" WHERE rowid > ? ORDER BY rowid LIMIT ?',
                (after, limit))
            replica, rows, last = Replica(conn), [], after
            for mapping in result.mappings():
                row = dict(mapping)
                last = row.pop("_sync_rowid")
                rows.append(SyncChange(seq=0, table_name=table, pk=replica.key_of(table, row), row=row))
            return {"rows": rows, "after": last, "more": len(rows) == limit, "max_seq": max_seq,
                    "max_hlc": _hlc_at(conn, max_seq) or ""}

        return await self._read(read)

    async def rows(self, keys: list[tuple[str, str]]) -> list[SyncChange]:
        def read(conn: Connection) -> list[SyncChange]:
            replica = Replica(conn)
            return [SyncChange(seq=0, table_name=table, pk=pk, row=replica.read(table, pk))
                    for table, pk in keys if table in SHARED_TABLES]

        return await self._read(read)

    async def prune(self, now: datetime | None = None) -> int:
        """Drop changes older than the retention, in chunks; raises the clock floor first."""
        cutoff = clock.encode(clock.Hlc(
            int(((now or utc_now()) - timedelta(days=TOMBSTONE_RETENTION_DAYS)).timestamp() * 1000), 0, ""))
        removed = 0
        engine = get_engine(self._db_url)
        while True:
            async with engine.begin() as conn:
                await conn.run_sync(raise_floor)
                upto = (await conn.exec_driver_sql(
                    "SELECT max(seq) FROM (SELECT seq FROM sync_changes WHERE hlc < ? "
                    "AND seq < (SELECT max(seq) FROM sync_changes) ORDER BY seq LIMIT ?)",
                    (cutoff, _PRUNE_CHUNK))).scalar()
                if upto is None:
                    return removed
                result = await conn.exec_driver_sql("DELETE FROM sync_changes WHERE seq <= ?", (upto,))
                await conn.exec_driver_sql(
                    "UPDATE sync_state SET pruned_seq = max(pruned_seq, ?) WHERE id = 1", (upto,))
                removed += result.rowcount or 0
