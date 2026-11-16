"""An edge's session face going up: what to send next, sending it, and what the answer means.

The worker drives it (aiteam.sync.worker); the API process reads the tail a forward
waits for (aiteam.sync.edge). A batch is read inside one read transaction, so its rows
and its ``snapshot`` describe the database at one moment: the center's stale check then
orders any two batches correctly, whichever process read them.

* ``read_batch``: the session rows changed after a position, through ``upto`` when
  an operation waits for no more than that (its ``session_seq``), each once in its
  latest form, deletions as None. Cut short at the batch limits.
* ``Uploader``: sends batches and records the answers. Only a 200 with a
  ``SyncSessionAck`` moves anything; a 5xx or a timeout waits; any other answer means
  the peer is no center to send to now (the edge detaches).
* ``take_ack`` / ``position_problem``: where this edge stands on the center, which is
  the authority on what it took in, within two bounds. A center behind what this edge
  pruned lost rows only a full resend (``resend-session``) restores; a center ahead of
  this edge's own sequence means this database went back (restored from a backup):
  the edge must not skip its own changes to catch up, so it stops and says so.
* ``prune``: session changes the center took in, older than the keep window.
* ``resend_pages`` / ``resent``: every session row again, for a center that lost
  more than this edge keeps (``python3 -m aiteam.sync resend-session``).
"""

from __future__ import annotations

import enum
import json
import threading
import time
from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import httpx
from sqlalchemy.engine import Connection, Engine

from aiteam.clock import utc_now
from aiteam.storage.partition import driver_sql, edge_origins
from aiteam.sync import hlc as clock
from aiteam.sync import ledger
from aiteam.sync.changelog import latest_seq, raise_floor
from aiteam.sync.protocol import (
    SESSION_BATCH_BYTES,
    SESSION_BATCH_ROWS,
    SESSION_CHANGES_KEEP_HOURS,
    UPLOAD_CYCLE_BATCHES,
    UPLOAD_CYCLE_S,
    timing,
)
from aiteam.sync.rows import Rows, quote
from aiteam.sync.tables import SESSION_TABLES, SESSION_TABLES_SQL
from aiteam.types import SyncChange, SyncSessionAck, SyncSessionBatch

_PRUNE_CHUNK = 5000
SEND_TIMEOUT_S = 30.0


class Step(enum.Enum):
    """How far one step of the worker got (sending the queue or the session face)."""

    DONE = "done"          # nothing left to do now
    WAITING = "waiting"    # failed for now; tried again next cycle
    DETACHED = "detached"  # the peer gave no answer of a center: not one to sync with now


def begin_read(conn: Connection) -> None:
    """Start a read transaction: every read until it ends sees one moment of the database."""
    conn.exec_driver_sql("BEGIN")


def _size(row: dict[str, Any] | None) -> int:
    return len(json.dumps(row, default=str, ensure_ascii=False)) if row is not None else 4


def read_batch(conn: Connection, origin: str, after: int, *, upto: int | None = None,
               max_rows: int = SESSION_BATCH_ROWS, max_bytes: int = SESSION_BATCH_BYTES) -> SyncSessionBatch:
    """The next batch after ``after``; call it at the start of a fresh read transaction."""
    begin_read(conn)
    snapshot = latest_seq(conn)
    limit = snapshot if upto is None else min(upto, snapshot)
    entries = conn.exec_driver_sql(
        f"SELECT seq, table_name, pk FROM sync_changes WHERE seq > ? AND seq <= ? "
        f"AND table_name IN ({SESSION_TABLES_SQL}) ORDER BY seq LIMIT ?", (after, limit, max_rows)).all()
    rows = Rows(conn, SESSION_TABLES)
    latest: dict[tuple[str, str], tuple[int, dict[str, Any] | None]] = {}
    size, reached = 0, limit
    for index, (seq, table, pk) in enumerate(entries):
        key = (table, pk)
        if key in latest:
            latest[key] = (seq, latest[key][1])
            continue
        row = rows.read(table, pk)
        if latest and size + _size(row) > max_bytes:
            reached = entries[index - 1][0]
            break
        size += _size(row)
        latest[key] = (seq, row)
    else:
        if len(entries) == max_rows:
            reached = entries[-1][0]
    changes = [SyncChange(seq=seq, table_name=table, pk=pk, row=row)
               for (table, pk), (seq, row) in sorted(latest.items(), key=lambda item: item[1][0])]
    return SyncSessionBatch(origin_node=origin, after=after, upto=max(reached, after), snapshot=snapshot,
                            changes=changes)


def backlog(conn: Connection, after: int, cap: int) -> int:
    """Session changes after ``after``, counted up to ``cap`` (enough to compare with a limit)."""
    return conn.exec_driver_sql(
        f"SELECT count(*) FROM (SELECT 1 FROM sync_changes WHERE seq > ? AND table_name IN ({SESSION_TABLES_SQL}) "
        "LIMIT ?)", (after, cap)).scalar_one()


def oldest_pending_hlc(conn: Connection, after: int) -> str | None:
    return conn.exec_driver_sql(
        f"SELECT hlc FROM sync_changes WHERE seq > ? AND table_name IN ({SESSION_TABLES_SQL}) ORDER BY seq LIMIT 1",
        (after,)).scalar()


def position_problem(conn: Connection, center_cursor: int) -> str:
    """What is wrong with standing at ``center_cursor`` (the center's word), or ""."""
    pruned = conn.exec_driver_sql("SELECT up_pruned FROM sync_peers WHERE peer = 'center'").scalar() or 0
    if center_cursor < pruned:
        return (f"中心的本机会话面只到序号 {center_cursor}，本机已清理到 {pruned}：中心库可能被旧备份恢复，"
                "需整份重传（python3 -m aiteam.sync resend-session）")
    own = latest_seq(conn)
    if center_cursor > own:
        return (f"中心记下本机会话面已到序号 {center_cursor}，本机库的序号只到 {own}：本机库可能被旧备份恢复，"
                "这之后本机写下的会话不会被跳过，需人工处理（核对后 python3 -m aiteam.sync resend-session）")
    return ""


class PositionError(RuntimeError):
    """The center and this edge disagree on where its session face stands; the message says how."""


def take_ack(conn: Connection, center_cursor: int) -> None:
    """Stand where the center says this edge stands; raises PositionError when that cannot be."""
    problem = position_problem(conn, center_cursor)
    if problem:
        raise PositionError(problem)
    conn.exec_driver_sql("UPDATE sync_peers SET up_cursor = ? WHERE peer = 'center'", (center_cursor,))


class Uploader:
    """Sends this edge's session face to its center, batch by batch."""

    def __init__(self, engine: Engine, client: httpx.Client, node_id: str, stop: threading.Event) -> None:
        self.engine = engine
        self.client = client
        self.node_id = node_id
        self.stop = stop

    def _write(self, work: Any, *args: Any) -> Any:
        with self.engine.begin() as conn:
            return work(conn, *args)

    def upload(self, upto: int | None = None) -> Step:
        """Send the session face up to ``upto`` and no further (everything when None, a
        bounded amount per cycle). DONE once there, or once the cycle's share is sent."""
        started, sent = time.monotonic(), 0
        while not self.stop.is_set():
            with self.engine.connect() as conn:
                peer = ledger.read_peer(conn)
                if upto is not None and peer.up_cursor >= upto:
                    return Step.DONE
                batch = read_batch(conn, self.node_id, peer.up_cursor, upto=upto)
            if batch.upto <= peer.up_cursor and not batch.changes:
                return Step.DONE
            if upto is None and (sent >= UPLOAD_CYCLE_BATCHES
                                 or time.monotonic() - started >= timing("upload_cycle", UPLOAD_CYCLE_S)):
                return Step.DONE
            step = self.send(batch)
            if step is not Step.DONE:
                return step
            if self._position() == peer.up_cursor and batch.upto > peer.up_cursor:
                return Step.WAITING  # answered without moving (a stale batch twice): next cycle
            sent += 1
        return Step.WAITING

    def _position(self) -> int:
        with self.engine.connect() as conn:
            return ledger.read_peer(conn).up_cursor

    def send(self, batch: SyncSessionBatch) -> Step:
        """Send one batch and record the answer."""
        try:
            response = self.client.post("/api/sync/session", content=batch.model_dump_json(),
                                        headers={"content-type": "application/json"},
                                        timeout=timing("replay_timeout", SEND_TIMEOUT_S))
        except (httpx.ConnectError, httpx.ConnectTimeout):
            self._write(ledger.note_error, "会话面上行未连上中心")
            raise
        except httpx.HTTPError as exc:
            self._write(ledger.note_error, f"会话面上行未完成：{exc}")
            return Step.WAITING
        if response.status_code >= 500:
            self._write(ledger.note_error, f"中心暂时无法接收会话面（{response.status_code}）：{response.text[:300]}")
            return Step.WAITING
        ack = _ack(response)
        if ack is None:
            reason = f"中心对会话面上行的应答不是确认（{response.status_code}）：{response.text[:300]}"
            self._write(ledger.detach, reason)
            return Step.DETACHED
        try:
            self._write(take_ack, ack.cursor)
        except PositionError as exc:
            self._write(ledger.detach, str(exc))
            return Step.DETACHED
        return Step.DONE


def _ack(response: httpx.Response) -> SyncSessionAck | None:
    if response.status_code != 200:
        return None
    try:
        return SyncSessionAck.model_validate_json(response.content)
    except ValueError:
        return None


def rewind(conn: Connection, center_cursor: int) -> bool:
    """The handshake found the center behind this edge's position: send again from where the
    center stands (within what this edge keeps). True when the position moved."""
    current = ledger.read_peer(conn).up_cursor
    if center_cursor >= current or position_problem(conn, center_cursor):
        return False
    conn.exec_driver_sql("UPDATE sync_peers SET up_cursor = ? WHERE peer = 'center'", (center_cursor,))
    return True


def prune(conn: Connection, now_ms: int | None = None) -> int:
    """Drop session changes the center took in, older than the keep window; returns how many.

    Raises the clock floor first: the log's last row may go.
    """
    up_cursor = conn.exec_driver_sql("SELECT up_cursor FROM sync_peers WHERE peer = 'center'").scalar() or 0
    keep_from = (now_ms if now_ms is not None
                 else int((utc_now() - timedelta(hours=SESSION_CHANGES_KEEP_HOURS)).timestamp() * 1000))
    cutoff = clock.encode(clock.Hlc(keep_from, 0, ""))
    upto = conn.exec_driver_sql(
        f"SELECT max(seq) FROM (SELECT seq FROM sync_changes WHERE seq <= ? AND hlc < ? "
        f"AND table_name IN ({SESSION_TABLES_SQL}) ORDER BY seq LIMIT ?)", (up_cursor, cutoff, _PRUNE_CHUNK)).scalar()
    if upto is None:
        return 0
    raise_floor(conn)
    removed = conn.exec_driver_sql(
        f"DELETE FROM sync_changes WHERE seq <= ? AND table_name IN ({SESSION_TABLES_SQL})", (upto,)).rowcount
    conn.exec_driver_sql("UPDATE sync_peers SET up_pruned = max(up_pruned, ?) WHERE peer = 'center'", (upto,))
    return removed or 0


def resend_pages(conn: Connection, origin: str, *, page: int = SESSION_BATCH_ROWS
                 ) -> Iterator[tuple[str, list[SyncChange]]]:
    """Every session row of this node (and of the baseline) as it is now, table by table.

    For ``resend-session``: a center that lost what this edge already pruned gets it
    all again; the rows are images, so sending one twice changes nothing.
    """
    rows = Rows(conn, SESSION_TABLES)
    owned, owners = driver_sql(edge_origins(origin))
    for table in sorted(SESSION_TABLES):
        after = 0
        while True:
            found = conn.exec_driver_sql(
                f"SELECT rowid AS _sync_rowid, * FROM {quote(table)} WHERE {owned} AND rowid > ? "
                "ORDER BY rowid LIMIT ?", (*owners, after, page)).mappings().all()
            if not found:
                break
            changes = []
            for mapping in found:
                row = dict(mapping)
                after = row.pop("_sync_rowid")
                changes.append(SyncChange(seq=0, table_name=table, pk=rows.key_of(table, row), row=row))
            yield table, changes
            if len(found) < page:
                break


def resent(conn: Connection, center_cursor: int) -> None:
    """After a full resend: the center has every row as of now, from ``center_cursor`` on."""
    conn.exec_driver_sql(
        "UPDATE sync_peers SET up_cursor = ?, up_pruned = min(up_pruned, ?) WHERE peer = 'center'",
        (center_cursor, center_cursor))
