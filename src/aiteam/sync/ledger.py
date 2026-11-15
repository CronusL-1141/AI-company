"""An edge's bookkeeping: its link to the center and the queue of its operations.

Synchronous functions over a SQLAlchemy connection, inside the caller's transaction:
the worker calls them directly, the API process through ``conn.run_sync``. This is
the one place that reads and writes ``sync_peers`` and maps the center's answer onto
an operation's state, so the API's forwards and the worker's replays settle alike.

States (aiteam.types.OpState): an operation stays ``pending`` until the center
answers it with a ``SyncOpResult``; nothing else settles it. ``dropped`` is for a
write that failed here before anything was staged or sent.
"""

from __future__ import annotations

from typing import Any, NamedTuple

from sqlalchemy.engine import Connection

from aiteam.clock import utc_now
from aiteam.sync.changelog import next_hlc
from aiteam.sync.merge import MergeRule, base_of
from aiteam.sync.protocol import CENTER_PEER
from aiteam.sync.replica import Replica
from aiteam.types import LinkState, OpOutcome, OpState, SyncOperation, SyncOpResult


class Peer(NamedTuple):
    """The edge's row about its center."""

    center_node_id: str
    link: LinkState
    successes: int
    failures: int
    cursor: int
    cursor_hlc: str
    handshake_error: str
    last_error: str
    last_ok_at: str | None


class Backlog(NamedTuple):
    """The operations still waiting for the center, and how the oldest is faring."""

    pending: int
    oldest_at: str | None
    head_attempts: int
    head_error: str


_PEER_COLUMNS = ("center_node_id, link, successes, failures, cursor, cursor_hlc, handshake_error, last_error, "
                 "last_ok_at")


def _now() -> str:
    return utc_now().isoformat()


def ensure_peer(conn: Connection) -> None:
    conn.exec_driver_sql(
        "INSERT OR IGNORE INTO sync_peers (peer, center_node_id, link, successes, failures, cursor, cursor_hlc, "
        "handshake_error, last_error, updated_at) VALUES (?, '', 'detached', 0, 0, 0, '', '', '', ?)",
        (CENTER_PEER, _now()))


def read_peer(conn: Connection) -> Peer:
    row = conn.exec_driver_sql(f"SELECT {_PEER_COLUMNS} FROM sync_peers WHERE peer = ?", (CENTER_PEER,)).first()
    if row is None:
        return Peer("", LinkState.DETACHED, 0, 0, 0, "", "", "", None)
    return Peer(row[0], LinkState(row[1]), *row[2:])


def write_peer(conn: Connection, peer: Peer) -> None:
    conn.exec_driver_sql(
        "UPDATE sync_peers SET center_node_id = ?, link = ?, successes = ?, failures = ?, cursor = ?, "
        "cursor_hlc = ?, handshake_error = ?, last_error = ?, last_ok_at = ?, updated_at = ? WHERE peer = ?",
        (peer.center_node_id, peer.link.value, peer.successes, peer.failures, peer.cursor, peer.cursor_hlc,
         peer.handshake_error, peer.last_error, peer.last_ok_at, _now(), CENTER_PEER))


def detach(conn: Connection, reason: str) -> None:
    """Go offline at once: the next forward stages, the worker probes before going back."""
    conn.exec_driver_sql(
        "UPDATE sync_peers SET link = 'detached', successes = 0, last_error = ?, updated_at = ? WHERE peer = ?",
        (reason, _now(), CENTER_PEER))


def note_error(conn: Connection, error: str) -> None:
    conn.exec_driver_sql("UPDATE sync_peers SET last_error = ?, updated_at = ? WHERE peer = ?",
                         (error, _now(), CENTER_PEER))


def advance_cursor(conn: Connection, cursor: int, cursor_hlc: str) -> None:
    conn.exec_driver_sql("UPDATE sync_peers SET cursor = ?, cursor_hlc = ? WHERE peer = ?",
                         (cursor, cursor_hlc, CENTER_PEER))


def record(conn: Connection, op: SyncOperation) -> SyncOperation:
    """Queue an operation under a fresh clock value, before anything else happens to it."""
    op = op.model_copy(update={"hlc": next_hlc(conn)})
    now = _now()
    conn.exec_driver_sql(
        "INSERT INTO sync_outbox (op_id, hlc, envelope, state, staged, outcome, attempts, last_error, "
        "created_at, updated_at) VALUES (?, ?, ?, 'pending', 0, '', 0, '', ?, ?)",
        (op.op_id, op.hlc, op.model_dump_json(), now, now))
    return op


def settle(conn: Connection, op_id: str, result: SyncOpResult) -> None:
    """Record the center's answer to a pending operation; the one mapping of answers to states."""
    state = OpState.CONFLICT if result.outcome is OpOutcome.CONFLICT else OpState.ACKED
    conn.exec_driver_sql(
        "UPDATE sync_outbox SET state = ?, outcome = ?, ack_seq = ?, last_error = '', updated_at = ? "
        "WHERE op_id = ? AND state = ?",
        (state.value, result.outcome.value, result.ack_seq, _now(), op_id, OpState.PENDING.value))


def attempt_failed(conn: Connection, op_id: str, error: str) -> None:
    conn.exec_driver_sql(
        "UPDATE sync_outbox SET attempts = attempts + 1, last_error = ?, updated_at = ? WHERE op_id = ?",
        (error, _now(), op_id))
    note_error(conn, error)


def drop(conn: Connection, op_id: str, reason: str) -> None:
    """A write that failed here before anything was staged or sent: never replayed."""
    conn.exec_driver_sql(
        "UPDATE sync_outbox SET state = ?, last_error = ?, updated_at = ? WHERE op_id = ? AND state = ?",
        (OpState.DROPPED.value, reason, _now(), op_id, OpState.PENDING.value))


def backlog(conn: Connection, before_hlc: str | None = None, excluding: frozenset[str] = frozenset()) -> Backlog:
    """Pending operations (older than ``before_hlc``, other than ``excluding``), oldest first."""
    query = "SELECT op_id, created_at, attempts, last_error FROM sync_outbox WHERE state = 'pending'"
    params: tuple[Any, ...] = ()
    if before_hlc is not None:
        query, params = query + " AND hlc < ?", (before_hlc,)
    rows = [row for row in conn.exec_driver_sql(query + " ORDER BY hlc", params).all() if row[0] not in excluding]
    if not rows:
        return Backlog(0, None, 0, "")
    return Backlog(len(rows), rows[0][1], rows[0][2], rows[0][3])


def staged_keys(conn: Connection) -> set[tuple[str, str]]:
    return {(table, pk) for table, pk in conn.exec_driver_sql("SELECT table_name, pk FROM sync_tentative").all()}


def base_for(conn: Connection, rule: MergeRule, pk: str) -> dict[str, Any]:
    """What the writer saw of the row as the center last gave it (``SyncOperation.base``).

    A row this edge changed and the center has not confirmed yet holds the edge's own
    staged values, not the center's: the base is then the one the first of those
    operations recorded, and ``tentative`` tells the center not to trust a revision
    equal by coincidence but to merge field by field.
    """
    first = conn.exec_driver_sql(
        "SELECT o.envelope FROM sync_tentative t JOIN sync_outbox o ON o.op_id = t.op_id "
        "WHERE t.table_name = ? AND t.pk = ? ORDER BY o.hlc LIMIT 1", (rule.table, pk)).first()
    if first is not None:
        return {**SyncOperation.model_validate_json(first[0]).base, "tentative": True}
    return base_of(rule, Replica(conn).read(rule.table, pk))
