"""Hook events queued behind a busy DB must survive their client giving up.

Real uvicorn (httptools, as in production) on a temporary SQLite file. An outside
connection holds the write lock; a burst of PreToolUse events fills the DB slots
and the rest queue. Every client times out long before the lock is released.

uvicorn answers ``receive()`` on a closed connection with ``http.disconnect`` even
when the body is already buffered, so an event that was still queued when its
client left used to reach the routes body-less and die as an unlogged 400. The
concurrency middleware now reads hook bodies before queueing; these tests check
that every event lands, and that removing the pre-read brings the loss back.
"""

from __future__ import annotations

import asyncio
import json
import socket
import sqlite3
import threading
import time
import uuid

from aiteam.api import middleware as middleware_module

BURST = 12  # more than the 5 DB slots, so most of the burst queues
CLIENT_TIMEOUT = 1.5  # send_event.py's production timeout
LOCK_HOLD = 3.0


def _post_and_give_up(port: int, payload: dict) -> str:
    """Send one hook POST like send_event.py does, then abandon it at the client timeout."""
    body = json.dumps(payload).encode()
    head = (
        f"POST /api/hooks/event HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
        f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\n"
        "Connection: close\r\n\r\n"
    ).encode()
    with socket.create_connection(("127.0.0.1", port), timeout=CLIENT_TIMEOUT) as sock:
        sock.sendall(head + body)
        try:
            return sock.recv(64).decode(errors="replace").split("\r\n", 1)[0]
        except TimeoutError:
            return "timeout"


def _landed(database, markers: list[str]) -> set[str]:
    con = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        rows = con.execute("SELECT data FROM events WHERE type = 'cc.tool_use'").fetchall()
    finally:
        con.close()
    blob = "\n".join(row[0] for row in rows)
    return {marker for marker in markers if marker in blob}


def _run_burst(port: int, database) -> tuple[list[str], set[str], list[str]]:
    locker = sqlite3.connect(database, isolation_level=None, timeout=5)
    locker.execute("BEGIN IMMEDIATE")
    markers = [f"preread-{uuid.uuid4().hex[:12]}" for _ in range(BURST)]
    outcomes: list[str] = [""] * BURST

    def fire(index: int) -> None:
        outcomes[index] = _post_and_give_up(port, {
            "hook_event_name": "PreToolUse",
            "session_id": "synthetic-preread-session",
            "tool_name": "Bash",
            "tool_input": {"command": f"echo {markers[index]}", "description": markers[index]},
            "tool_use_id": f"toolu_{markers[index]}",
        })

    threads = [threading.Thread(target=fire, args=(i,)) for i in range(BURST)]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
        time.sleep(max(0.0, LOCK_HOLD - CLIENT_TIMEOUT))
    finally:
        locker.execute("COMMIT")
        locker.close()
    # Wait until everything landed, or nothing more has landed for a while.
    deadline = time.monotonic() + 15
    landed = _landed(database, markers)
    settled_at = time.monotonic()
    while len(landed) < BURST and time.monotonic() < deadline:
        if time.monotonic() - settled_at > 2.0:
            break
        time.sleep(0.2)
        now_landed = _landed(database, markers)
        if now_landed != landed:
            landed, settled_at = now_landed, time.monotonic()
    print(f"burst={BURST} landed={len(landed)} outcomes={sorted(set(outcomes))}")
    return markers, landed, outcomes


def test_queued_events_land_after_their_clients_left(hook_server):
    port, database, slots = hook_server
    gone_before = middleware_module.hook_ingest_stats["client_gone"]

    markers, landed, outcomes = _run_burst(port, database)

    assert outcomes == ["timeout"] * BURST  # every receipt was lost...
    assert landed == set(markers)  # ...and every event still landed
    # The queued part of the burst is exactly what hook_client_gone counts.
    assert middleware_module.hook_ingest_stats["client_gone"] - gone_before >= BURST - 5


def test_without_the_preread_queued_events_are_lost(hook_server, monkeypatch):
    """Reverse check: the same burst with the pre-read disabled loses the queued events."""
    port, database, slots = hook_server

    async def no_preread(request):
        return None, False

    monkeypatch.setattr(
        middleware_module.SQLiteConcurrencyMiddleware, "_preread_body", staticmethod(no_preread),
    )

    markers, landed, outcomes = _run_burst(port, database)

    assert outcomes == ["timeout"] * BURST
    # Only the events already holding a DB slot when their clients left survive;
    # everything that was still queued is lost.
    assert len(landed) <= slots, f"{len(landed)} landed, more than the {slots} DB slots"


def test_duplicate_delivery_after_a_lost_receipt_is_answered_not_reprocessed(hook_server):
    """A client that retries after a lost receipt must not record the event twice."""
    port, database, slots = hook_server
    marker = f"retry-{uuid.uuid4().hex[:12]}"
    payload = {
        "hook_event_name": "PreToolUse",
        "session_id": "synthetic-preread-session",
        "tool_name": "Bash",
        "tool_input": {"command": f"echo {marker}", "description": marker},
        "tool_use_id": f"toolu_{marker}",
    }
    first = _post_and_give_up(port, payload)
    second = _post_and_give_up(port, payload)
    assert first.startswith("HTTP/1.1 200") and second.startswith("HTTP/1.1 200")
    con = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        rows = con.execute(
            "SELECT COUNT(*) FROM events WHERE type = 'cc.tool_use' AND data LIKE ?", (f"%{marker}%",),
        ).fetchone()
    finally:
        con.close()
    assert rows == (1,)


def test_client_gone_before_the_preread_is_counted_as_a_lost_event(hook_server, monkeypatch):
    """The window the pre-read cannot close: the client leaves before dispatch reads the body.

    uvicorn then answers receive() with http.disconnect although the body is buffered,
    so the event is lost. It must at least be counted and reported.
    """
    port, database, _ = hook_server
    original = middleware_module.SQLiteConcurrencyMiddleware._preread_body
    reported: list[tuple[str, dict]] = []

    async def late_preread(request):
        await asyncio.sleep(0.8)  # a starved event loop reaching the request late
        return await original(request)

    monkeypatch.setattr(
        middleware_module.SQLiteConcurrencyMiddleware, "_preread_body", staticmethod(late_preread),
    )
    monkeypatch.setattr(middleware_module, "record_event", lambda event, **f: reported.append((event, f)))
    lost_before = middleware_module.hook_ingest_stats["body_lost"]
    marker = f"bodylost-{uuid.uuid4().hex[:12]}"
    body = json.dumps({
        "hook_event_name": "PreToolUse", "session_id": "synthetic-preread-session",
        "tool_name": "Bash", "tool_input": {"command": f"echo {marker}"},
    }).encode()
    with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
        sock.sendall(
            f"POST /api/hooks/event HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
            f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n".encode() + body
        )
    deadline = time.monotonic() + 5
    while middleware_module.hook_ingest_stats["body_lost"] == lost_before:
        assert time.monotonic() < deadline, "body loss was not counted"
        time.sleep(0.05)
    assert middleware_module.hook_ingest_stats["body_lost"] == lost_before + 1
    assert [event for event, _ in reported] == ["http.server.hook_body_lost"]
    assert _landed(database, [marker]) == set()
