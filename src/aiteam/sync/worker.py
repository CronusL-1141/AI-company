"""The edge's sync worker: a child of the API process, alive exactly as long as it.

Moving rows is batch work that must not compete with request handling for the event
loop or the GIL, so it runs here, in its own process, with its own connection.
Every probe interval it probes the link; every pull interval (and at once when the
API writes a line to its stdin) it works:

1. Probe the center (``GET /api/sync/hello``) and check the handshake: protocol,
   database clock convention, schema, the role and the node pinned on first
   contact, a change sequence that never goes back, the change at the cursor
   still being the one the edge applied, and the center still holding this edge's
   session face as far as the edge pruned it. Two failed probes detach, three good
   ones start draining, draining ends attached once the queue is empty and the
   session face is (nearly) up.
2. While attached or draining: replay the queue in clock order. Before each
   operation, send this node's session face up to the operation's ``session_seq``
   and no further: the center then sees what the edge saw when it wrote (its
   teams, its agents), not later rows. Only the center's verdict (200 with a
   SyncOpResult) settles an operation. A temporary failure (5xx, a timeout) leaves
   it at the head and the rest waits behind it; any other answer means the peer is
   not a center to sync with now: the edge detaches at once and the operation
   stays queued.
3. Send the rest of the session face, a bounded amount per cycle
   (aiteam.sync.upload: batches the center takes in only in order).
4. Whatever the queue does: pull the center's changes into the replica (rebuilding
   it when the center no longer keeps the tombstones the cursor needs, or when an
   operator asked), delete this node's session rows of a project the center
   deleted, and replace staged rows by the center's once the operations that
   staged them have the center's verdict; tell the center when a drain ends.

An idle worker reads and does not write: nothing is committed unless something
changed. Stdin closing (the API exited, however it exited) ends the worker. No
timers live beyond the API process: this is part of it, like its state reaper.

Usage: python -m aiteam.sync.worker --db PATH --node-id ID --center-url URL
"""

from __future__ import annotations

import argparse
import logging
import sys
import threading
import time
from datetime import timedelta
from typing import Any

import httpx
from sqlalchemy import create_engine, event
from sqlalchemy.engine import Connection, Engine

from aiteam.clock import parse_utc, utc_now
from aiteam.storage.cascade import delete_project_session_rows
from aiteam.storage.partition import edge_origins
from aiteam.sync import ledger, upload
from aiteam.sync.changelog import suppressed
from aiteam.sync.ledger import Peer
from aiteam.sync.protocol import (
    ATTACH_AFTER_SUCCESSES,
    CHANGES_PAGE,
    DETACH_AFTER_FAILURES,
    FORWARD_TAIL_ROWS,
    PROBE_INTERVAL_S,
    PROBE_TIMEOUT_S,
    PULL_INTERVAL_S,
    ROWS_PAGE,
    SETTLED_OP_RETENTION_DAYS,
    SYNC_PROTOCOL,
    schema_version,
    timing,
)
from aiteam.sync.replica import Replica
from aiteam.sync.tables import SHARED_TABLES
from aiteam.sync.upload import Step, Uploader
from aiteam.types import (
    LinkState,
    SyncChange,
    SyncChangesPage,
    SyncHello,
    SyncOperation,
    SyncOpResult,
)

logger = logging.getLogger("aiteam.sync.worker")

REPLAY_TIMEOUT_S = 30.0
# How often an unchanged, healthy link still refreshes last_ok_at, and settled
# operations are forgotten: rarely, so an idle edge does not write.
_REFRESH_OK_S = 60.0
_HOUSEKEEP_EVERY_S = 3600.0


class Worker:
    def __init__(self, engine: Engine, client: httpx.Client, node_id: str,
                 stop: threading.Event | None = None) -> None:
        self.engine = engine
        self.client = client
        self.node_id = node_id
        # Set when the API process is gone: a long drain ends between two steps.
        self.stop = stop or threading.Event()
        self.uploader = Uploader(engine, client, node_id, self.stop)
        self._housekept_at = -_HOUSEKEEP_EVERY_S

    # -- one cycle -----------------------------------------------------------

    def cycle(self, *, probe: bool) -> None:
        """One round of work; the link is probed only when ``probe`` (every probe interval)."""
        if probe:
            hello, problem = self.probe()
            link = self.record_probe(hello, problem)
            self.rewind_to(hello, problem)
        else:
            link = self.peer().link
        if link is LinkState.DETACHED:
            return
        try:
            replayed = self.replay()
            if replayed is Step.DETACHED:
                return
            # The rest of the session face goes up only behind an empty queue: while the
            # head waits, the center must not see rows past the head's position.
            if replayed is Step.DONE and self.uploader.upload() is Step.DETACHED:
                return
            self.pull()  # never held up by a queue that is stuck
            self.reconcile()
            self.finish_drain(link)
            self.housekeep()
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            self.record_probe(None, f"未连上中心：{exc}")  # as certain as a failed probe
        except httpx.HTTPError as exc:
            self._write(ledger.note_error, f"同步中断：{exc}")

    def peer(self) -> Peer:
        with self.engine.connect() as conn:
            return ledger.read_peer(conn)

    def _write(self, work: Any, *args: Any) -> Any:
        with self.engine.begin() as conn:
            return work(conn, *args)

    # -- the link ------------------------------------------------------------

    def probe(self) -> tuple[SyncHello | None, str]:
        peer = self.peer()
        params: dict[str, Any] = {"node": self.node_id}
        if peer.cursor > 0 and peer.cursor_hlc:
            params.update(cursor=peer.cursor, cursor_hlc=peer.cursor_hlc)
        try:
            response = self.client.get("/api/sync/hello", params=params,
                                       timeout=timing("probe_timeout", PROBE_TIMEOUT_S))
            response.raise_for_status()
            hello = SyncHello.model_validate_json(response.content)
        except (httpx.HTTPError, ValueError) as exc:
            return None, f"探活失败：{exc}"
        return hello, self.handshake_problem(hello, peer)

    def handshake_problem(self, hello: SyncHello, peer: Peer) -> str:
        with self.engine.connect() as conn:
            user_version = conn.exec_driver_sql("PRAGMA user_version").scalar_one()
        if hello.role != "center":
            return f"对端 {hello.node_id} 的角色是 {hello.role}，不是中心"
        if peer.center_node_id and hello.node_id != peer.center_node_id:
            return (f"中心身份变了：记下的是 {peer.center_node_id}，现在应答的是 {hello.node_id}，拒绝同步"
                    "（确认换了中心后运行 python3 -m aiteam.sync repin）")
        if hello.sync_protocol != SYNC_PROTOCOL:
            return f"同步协议不符（中心 {hello.sync_protocol}，本机 {SYNC_PROTOCOL}）：两台机器的 OS 版本需要一致"
        if hello.user_version != user_version:
            return f"数据库时钟制式不符（中心 {hello.user_version}，本机 {user_version}）"
        if hello.schema_version != schema_version():
            return f"同步表结构不符（中心 {hello.schema_version}，本机 {schema_version()}）：两台机器的 OS 版本需要一致"
        if hello.max_seq < peer.cursor or hello.cursor_known is False:
            return (f"中心的变更日志与本机记下的不符（中心序号 {hello.max_seq}，本机游标 {peer.cursor}）："
                    "中心库可能被旧备份恢复，需人工处理（python3 -m aiteam.sync repin --rebuild）")
        with self.engine.connect() as conn:
            return upload.position_problem(conn, hello.intake_cursor or 0)

    def record_probe(self, hello: SyncHello | None, problem: str) -> LinkState:
        """Apply one probe to the link state; written only when something changed."""
        current = self.peer()
        updated = _after_probe(current, hello, problem)
        if not _worth_writing(current, updated):
            return updated.link
        with self.engine.begin() as conn:
            # Under the write lock, from the row as it is now: the API may have detached.
            conn.exec_driver_sql("UPDATE sync_peers SET updated_at = updated_at")
            updated = _after_probe(ledger.read_peer(conn), hello, problem)
            ledger.write_peer(conn, updated)
        return updated.link

    def rewind_to(self, hello: SyncHello | None, problem: str) -> None:
        """A center that stands behind this edge's position (it lost the end of what it
        took in) is sent the rest again from where it stands, before any operation waits."""
        if hello is None or problem or hello.intake_cursor is None:
            return
        if hello.intake_cursor < self.peer().up_cursor:
            self._write(upload.rewind, hello.intake_cursor)

    # -- the queue -----------------------------------------------------------

    def replay(self) -> Step:
        """Send queued operations in order until the queue is empty or the head cannot go."""
        while not self.stop.is_set():
            with self.engine.connect() as conn:
                head = conn.exec_driver_sql(
                    "SELECT op_id, envelope FROM sync_outbox WHERE state = 'pending' ORDER BY hlc LIMIT 1").first()
            if head is None:
                return Step.DONE
            op_id, envelope = head
            waits_for = SyncOperation.model_validate_json(envelope).session_seq
            sent = self.uploader.upload(upto=waits_for)
            if sent is not Step.DONE:
                return sent
            try:
                response = self.client.post("/api/sync/ops", content=envelope,
                                            headers={"content-type": "application/json"},
                                            timeout=timing("replay_timeout", REPLAY_TIMEOUT_S))
            except (httpx.ConnectError, httpx.ConnectTimeout):
                self._write(ledger.attempt_failed, op_id, "重放未连上中心")
                raise
            except httpx.HTTPError as exc:
                self._write(ledger.attempt_failed, op_id, f"重放未完成：{exc}")
                return Step.WAITING
            if response.status_code >= 500:
                self._write(ledger.attempt_failed, op_id,
                            f"中心暂时无法执行（{response.status_code}）：{response.text[:300]}")
                return Step.WAITING
            result = _verdict(response)
            if result is None:
                reason = f"中心对重放的应答不是裁决（{response.status_code}）：{response.text[:300]}"
                self._write(ledger.attempt_failed, op_id, reason)
                self._write(ledger.detach, reason)
                return Step.DETACHED
            self._write(ledger.settle, op_id, result)
        return Step.WAITING

    # -- the replica ---------------------------------------------------------

    def pull(self) -> None:
        while not self.stop.is_set():
            cursor = self.peer().cursor
            if cursor < 0:  # an operator asked for a rebuild (repin --rebuild)
                self.rebuild()
                continue
            response = self.client.get("/api/sync/changes", params={"after": cursor, "limit": CHANGES_PAGE},
                                       timeout=REPLAY_TIMEOUT_S)
            response.raise_for_status()
            page = SyncChangesPage.model_validate_json(response.content)
            if page.rebuild:
                self.rebuild()
                continue
            self.apply(page)
            if page.upto >= page.max_seq or page.upto <= cursor:
                return

    def apply(self, page: SyncChangesPage) -> None:
        """Write a page into the replica, unless it brings nothing past the cursor."""
        if page.upto <= self.peer().cursor:
            return
        with self.engine.begin() as conn:
            with suppressed(conn):
                cursor = ledger.read_peer(conn).cursor
                if page.upto <= cursor:
                    return
                protected = ledger.staged_keys(conn)
                replica = Replica(conn)
                for change in page.changes:
                    if change.seq > cursor and (change.table_name, change.pk) not in protected:
                        replica.write(change.table_name, change.pk, change.row)
                ledger.advance_cursor(conn, page.upto, page.upto_hlc)
            self._cascade_deleted_projects(conn, [c.pk for c in page.changes
                                                  if c.table_name == "projects" and c.row is None])

    def _cascade_deleted_projects(self, conn: Connection, project_ids: list[str]) -> None:
        """This node's session rows of projects the center deleted go too, as this node's
        own writes (not suppressed: they reach the center with the session face)."""
        for project_id in project_ids:
            delete_project_session_rows(conn, project_id, edge_origins(self.node_id))

    def rebuild(self) -> None:
        """Replace the whole shared face with the center's (its tombstones are gone)."""
        snapshot: tuple[int, str] | None = None
        for table in sorted(SHARED_TABLES):
            seen: set[str] = set()
            after = 0
            while True:
                response = self.client.get("/api/sync/snapshot", params={"table": table, "after": after,
                                                                         "limit": CHANGES_PAGE},
                                           timeout=REPLAY_TIMEOUT_S)
                response.raise_for_status()
                page = response.json()
                if snapshot is None or page["max_seq"] < snapshot[0]:
                    snapshot = (page["max_seq"], page["max_hlc"])
                rows = [SyncChange.model_validate(item) for item in page["rows"]]
                with self.engine.begin() as conn, suppressed(conn):
                    protected = ledger.staged_keys(conn)
                    replica = Replica(conn)
                    for change in rows:
                        seen.add(change.pk)
                        if (table, change.pk) not in protected:
                            replica.write(table, change.pk, change.row)
                if not page["more"]:
                    break
                after = page["after"]
            with self.engine.begin() as conn:
                with suppressed(conn):
                    protected = ledger.staged_keys(conn)
                    replica = Replica(conn)
                    gone = [pk for pk in replica.keys(table) - seen if (table, pk) not in protected]
                    for pk in gone:
                        replica.write(table, pk, None)
                if table == "projects":
                    self._cascade_deleted_projects(conn, gone)
        self._write(ledger.advance_cursor, *(snapshot or (0, "")))

    def reconcile(self) -> None:
        """Replace staged rows by the center's once the center has a verdict on every
        operation that staged them and the replica has caught up with it.

        Only the registrations taken into account are removed: an operation staged on
        the same row meanwhile keeps its row and its registration.
        """
        with self.engine.connect() as conn:
            cursor = ledger.read_peer(conn).cursor
            registrations = conn.exec_driver_sql(
                "SELECT t.table_name, t.pk, t.op_id, o.state IN ('acked', 'conflict') AND o.ack_seq IS NOT NULL "
                "AND o.ack_seq <= ? FROM sync_tentative t LEFT JOIN sync_outbox o ON o.op_id = t.op_id",
                (cursor,)).all()
        settled: dict[tuple[str, str], set[str]] = {}
        unsettled: set[tuple[str, str]] = set()
        for table, pk, op_id, done in registrations:
            if done:
                settled.setdefault((table, pk), set()).add(op_id)
            else:
                unsettled.add((table, pk))
        keys = [key for key in settled if key not in unsettled]
        for start in range(0, len(keys), ROWS_PAGE):
            self._settle_rows(keys[start:start + ROWS_PAGE], settled)

    def _settle_rows(self, keys: list[tuple[str, str]], settled: dict[tuple[str, str], set[str]]) -> None:
        response = self.client.post("/api/sync/rows", json={"keys": [list(key) for key in keys]},
                                    timeout=REPLAY_TIMEOUT_S)
        response.raise_for_status()
        rows = {(c.table_name, c.pk): c.row for c in
                (SyncChange.model_validate(item) for item in response.json()["rows"])}
        with self.engine.begin() as conn, suppressed(conn):
            replica = Replica(conn)
            for table, pk in keys:
                now = {op for (op,) in conn.exec_driver_sql(
                    "SELECT op_id FROM sync_tentative WHERE table_name = ? AND pk = ?", (table, pk)).all()}
                if now - settled[(table, pk)]:
                    continue  # staged again meanwhile: its own operation settles it
                replica.write(table, pk, rows.get((table, pk)))
                for op_id in now:
                    conn.exec_driver_sql("DELETE FROM sync_tentative WHERE table_name = ? AND pk = ? AND op_id = ?",
                                         (table, pk, op_id))

    def finish_drain(self, link: LinkState) -> None:
        """Draining ends once the queue is empty and the session face is up but for at most
        one forward's tail (a forward with more behind it would be staged)."""
        if link is not LinkState.DRAINING:
            return
        with self.engine.connect() as conn:
            if ledger.backlog(conn).pending:
                return
            behind = upload.backlog(conn, ledger.read_peer(conn).up_cursor, FORWARD_TAIL_ROWS + 1)
            if behind > FORWARD_TAIL_ROWS:
                return
        self.client.post("/api/sync/drained", json={"node_id": self.node_id}, timeout=REPLAY_TIMEOUT_S)
        with self.engine.begin() as conn:
            conn.exec_driver_sql(
                "UPDATE sync_peers SET link = ?, last_error = '', updated_at = ? WHERE link = ?",
                (LinkState.ATTACHED.value, utc_now().isoformat(), LinkState.DRAINING.value))

    def housekeep(self) -> None:
        """Once an hour: forget settled operations, prune session changes the center has."""
        if time.monotonic() - self._housekept_at < _HOUSEKEEP_EVERY_S:
            return
        self._housekept_at = time.monotonic()
        self.forget_settled()
        while self._write(upload.prune):
            pass

    def forget_settled(self) -> None:
        """Settled operations are kept a while for diagnosis."""
        cutoff = (utc_now() - timedelta(days=SETTLED_OP_RETENTION_DAYS)).isoformat()
        condition = ("state != 'pending' AND updated_at < ? AND op_id NOT IN (SELECT op_id FROM sync_tentative)")
        with self.engine.connect() as conn:
            if conn.exec_driver_sql(f"SELECT 1 FROM sync_outbox WHERE {condition} LIMIT 1", (cutoff,)).first() is None:
                return
        with self.engine.begin() as conn:
            conn.exec_driver_sql(f"DELETE FROM sync_outbox WHERE {condition}", (cutoff,))


def _verdict(response: httpx.Response) -> SyncOpResult | None:
    """The center's verdict: 200 and parseable, or nothing that may settle an operation."""
    if response.status_code != 200:
        return None
    try:
        return SyncOpResult.model_validate_json(response.content)
    except ValueError:
        return None


def _after_probe(peer: Peer, hello: SyncHello | None, problem: str) -> Peer:
    """The link after one probe: two failures detach, a mismatched handshake at once, three
    good probes start draining. The counters stop at their thresholds, so a steady link
    (healthy, or down for days) is a fixed point and writes nothing."""
    ok = hello is not None and not problem
    successes, failures = ((min(peer.successes + 1, ATTACH_AFTER_SUCCESSES), 0) if ok
                           else (0, min(peer.failures + 1, DETACH_AFTER_FAILURES)))
    link = peer.link
    if not ok and (failures >= DETACH_AFTER_FAILURES or hello is not None):
        link = LinkState.DETACHED
    elif ok and link is LinkState.DETACHED and successes >= ATTACH_AFTER_SUCCESSES:
        link = LinkState.DRAINING
    pinned, cursor = peer.center_node_id, peer.cursor
    if ok and not pinned:
        pinned = hello.node_id  # type: ignore[union-attr]
        cursor = cursor if cursor < 0 else max(cursor, hello.baseline_seq)  # type: ignore[union-attr]
    return peer._replace(
        center_node_id=pinned, link=link, successes=successes, failures=failures, cursor=cursor,
        handshake_error=problem if hello is not None else "",
        last_error=problem if hello is None else peer.last_error,
        last_ok_at=utc_now().isoformat() if ok else peer.last_ok_at,
    )


def _worth_writing(current: Peer, updated: Peer) -> bool:
    if updated._replace(last_ok_at=current.last_ok_at) != current:
        return True
    last = parse_utc(current.last_ok_at)
    return updated.last_ok_at != current.last_ok_at and (
        last is None or (utc_now() - last).total_seconds() >= _REFRESH_OK_S)


def open_engine(db_path: str) -> Engine:
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"timeout": 30})

    @event.listens_for(engine, "connect")
    def _pragmas(dbapi_conn: Any, _record: Any) -> None:
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.close()

    return engine


def _watch_stdin(wake: threading.Event, stop: threading.Event) -> None:
    for _line in sys.stdin.buffer:
        wake.set()
    stop.set()
    wake.set()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m aiteam.sync.worker")
    parser.add_argument("--db", required=True)
    parser.add_argument("--node-id", required=True)
    parser.add_argument("--center-url", required=True)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s sync-worker %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    wake, stop = threading.Event(), threading.Event()
    threading.Thread(target=_watch_stdin, args=(wake, stop), daemon=True).start()
    engine = open_engine(args.db)
    probe_every, pull_every = timing("probe_interval", PROBE_INTERVAL_S), timing("pull_interval", PULL_INTERVAL_S)
    last_probe = -probe_every
    with httpx.Client(base_url=args.center_url, trust_env=False) as client:
        worker = Worker(engine, client, args.node_id, stop)
        while not stop.is_set():
            probe = time.monotonic() - last_probe >= probe_every
            try:
                worker.cycle(probe=probe)
            except Exception:  # noqa: BLE001 - one bad cycle must not end the sync
                logger.exception("sync cycle failed")
            if probe:
                last_probe = time.monotonic()
            wake.wait(timeout=pull_every)
            wake.clear()
    engine.dispose()
    return 0


if __name__ == "__main__":
    sys.exit(main())
