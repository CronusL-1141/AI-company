#!/usr/bin/env python3
"""AI Team OS - the one way a Claude Code hook delivers an event to the OS API.

Every CC hook that POSTs to ``/api/hooks/event`` goes through ``post_body`` here
(machine-checked by I25 in ``scripts/check_invariants.sh``). A failed POST is
never silent: it is sorted into one of ``FAILURE_CLASSES`` and appended to a
local ledger that ``os_health_check`` reads, so a lost event leaves a trace even
while the API is down.

Why the classes matter: "timed out after the request was sent" usually means the
event landed and only the receipt was lost, "connection refused" means it never
arrived, and an HTTP 4xx means the server read it and said no. The old entry
code wrote all of them as "API unreachable", which made a loss rate unreadable.

This file is frozen together with ``send_event.py`` (``scripts/hook_entry_freeze.json``,
I1c): CC delivery behaviour changes only through a reviewed golden diff.

Rules for this module: standard library only; no threads, timers, subprocesses or
forks (I25) - a hook runs, delivers, and exits; it never raises into its host.
"""

import http.client
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime

POST_TIMEOUT = 1.5
LEDGER_NAME = "ledger.jsonl"
LEDGER_ROTATED_NAME = "ledger.1.jsonl"
LEDGER_MAX_BYTES = 1_000_000

POSTED = "posted"
# Size coupling: "connect_failed means the event did not arrive" holds only while
# the body fits in the kernel send buffer, so urllib's connect-and-send phase ends
# before the server has to read anything. Measured on macOS loopback: bodies up to
# 300KB leave that phase intact, 1MB does not. Bodies here stay near 50KB at most
# (the 32KB strip gate plus the 50KB truncation). Re-measure before raising
# MAX_PAYLOAD_BYTES or the truncation limits in send_event.py / hook_core.py.
FAILURE_CLASSES = (
    "http_4xx",            # the server read the event and rejected it
    "http_5xx",            # the handler failed; the event may be partly recorded
    "refused",             # nothing listening: the event certainly did not arrive
    "connect_failed",      # failed while connecting or sending: did not arrive
    "timeout_after_send",  # sent, no answer in time: usually landed, receipt lost
    "reset_after_send",    # sent, then the connection broke: outcome unknown
    "other",               # anything else, including encoding errors
)


def classify_post_failure(exc: BaseException) -> tuple[str, int | None]:
    """Sort a failed hook POST into one of the failure classes, with its HTTP status.

    Order matters: HTTPError is a subclass of URLError, and urllib wraps only the
    connect-and-send phase in URLError. Exceptions raised while waiting for the
    response (a read timeout, a dropped connection) come through unwrapped.
    """
    if isinstance(exc, urllib.error.HTTPError):
        if 400 <= exc.code < 500:
            return "http_4xx", exc.code
        if 500 <= exc.code < 600:
            return "http_5xx", exc.code
        return "other", exc.code
    if isinstance(exc, urllib.error.URLError):
        if isinstance(exc.reason, ConnectionRefusedError):
            return "refused", None
        return "connect_failed", None
    if isinstance(exc, TimeoutError):
        return "timeout_after_send", None
    if isinstance(exc, (ConnectionError, http.client.HTTPException)):
        return "reset_after_send", None
    return "other", None


def ledger_dir() -> str:
    """Where the delivery ledger lives; resolved per call so HOME overrides apply."""
    return os.path.join(os.path.expanduser("~"), ".claude", "data", "ai-team-os", "hook-delivery")


def _record(entry: dict) -> None:
    """Append one ledger line; never raises.

    One ``os.write`` on an O_APPEND descriptor per line, so concurrent hook
    processes never interleave inside a line. Past LEDGER_MAX_BYTES the file is
    renamed to LEDGER_ROTATED_NAME (one generation kept) by ``_rotate``.
    """
    try:
        directory = ledger_dir()
        os.makedirs(directory, mode=0o700, exist_ok=True)
        path = os.path.join(directory, LEDGER_NAME)
        line = (json.dumps(entry, separators=(",", ":")) + "\n").encode("utf-8")
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, line)
            written = os.fstat(fd)
        finally:
            os.close(fd)
        if written.st_size > LEDGER_MAX_BYTES:
            _rotate(directory, path, written.st_ino)
    except Exception:
        pass


def _rotate(directory: str, path: str, inode: int) -> None:
    """Rename the oversized ledger aside, at most once per generation.

    Many writers cross the size limit together. Checking "is the path still the
    file I wrote to" and then renaming is two steps, and between them another
    writer can rotate and a third can create a fresh ledger - which the late
    rename would then move over the rotated generation, losing it. So the check
    and the rename happen under an exclusive lock, taken without waiting: a writer
    that cannot get it leaves the rotation to the one that did.
    """
    try:
        import fcntl
    except ImportError:  # no flock (Windows): rotate unguarded rather than grow forever
        fcntl = None
    lock_fd = os.open(os.path.join(directory, ".rotate.lock"), os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        if fcntl is not None:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return
        current = os.stat(path)
        if current.st_ino == inode and current.st_size > LEDGER_MAX_BYTES:
            os.replace(path, os.path.join(directory, LEDGER_ROTATED_NAME))
    finally:
        os.close(lock_fd)


def post_body(
    data: bytes, api_url: str, event_name: str, *,
    session_id: str = "", tool_use_id: str = "", timeout: float = POST_TIMEOUT,
) -> str:
    """POST one already-serialized hook body. Returns POSTED or a failure class.

    Never raises. A failure writes one stderr line and one ledger line. The ledger
    holds identifiers and timing only, never payload content.
    """
    started = time.monotonic()
    try:
        req = urllib.request.Request(
            f"{api_url}/api/hooks/event",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp.read()  # Consume response without output - decisions handled by workflow_reminder.py
        return POSTED
    except Exception as exc:
        cls, status = classify_post_failure(exc)
        elapsed_ms = int((time.monotonic() - started) * 1000)
        sys.stderr.write(
            f"[aiteam-hook] {event_name}: post_failed cls={cls} status={status or '-'} - {exc}\n"
        )
        _record({
            "t": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "ev": event_name,
            "cls": cls,
            "status": status,
            "ms": elapsed_ms,
            "sid": session_id if isinstance(session_id, str) else "",
            "keyed": bool(tool_use_id) and isinstance(tool_use_id, str),
        })
        return cls
