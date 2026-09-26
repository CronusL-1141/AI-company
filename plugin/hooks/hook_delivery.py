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

A failed tool event that carries a ``tool_use_id`` is also put in a local replay
queue and redelivered, marked ``_hook_replay``, by a later hook whose own POST just
went through quickly (see "Replay queue" below). The server deduplicates those
events on (session_id, hook_event_name, tool_use_id), so redelivering one that did
land costs a receipt lookup, not a second record. Events without that key, and
lifecycle events, are only recorded: a late copy of them has no safe meaning.

This file is frozen together with ``send_event.py`` (``scripts/hook_entry_freeze.json``,
I1c): CC delivery behaviour changes only through a reviewed golden diff.

Rules for this module: standard library only; no threads, timers, subprocesses or
forks (I25) - a hook runs, delivers, and exits; it never raises into its host.
"""

import hashlib
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
# Size note: hook bodies are not small by construction. send_event.py truncates a
# few named fields, but tool_input passes whole (a 1MB Write sends about 1MB), and
# the 32KB strip gate keeps tool_input too. What the classes below promise still
# holds at any size: connect_failed is raised while the request is still being
# sent, and the server handles no event whose body did not arrive complete. What
# size changes is where a slow send fails: on macOS loopback bodies up to 300KB
# leave urllib's send phase at once, 1MB does not. The replay queue caps what it
# stores (MAX_RECORD_BYTES) for its own reasons, see below.
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


# ---------------------------------------------------------------------------
# Replay queue
# ---------------------------------------------------------------------------
# A maildir under ledger_dir()/spool. A record is written to tmp/ and renamed into
# new/. A drain claims it by renaming new/X to cur/X.<token>, which exactly one
# process can do; the token is unique per claim, so a claim that was recovered as
# an orphan and claimed again cannot be mistaken for the old one. It then posts the
# record and deletes it, or puts it back by renaming its own cur/ file out of the
# way first (which fails if it no longer holds the claim). No locks, no background
# work: the queue only moves when a hook runs.
#
# What goes in: PreToolUse / PostToolUse / PostToolUseFailure with a session_id and
# tool_use_id, after any failure except http_4xx (the server read it and said no)
# and other (it was never sent). Everything else is recorded in the ledger only.
# A record is keyed by its origin time (the FIFO order) and the event identity; the
# same event failing twice would give two records, and the server's receipt dedupe
# makes the second delivery a no-op.
REPLAYABLE_EVENTS = frozenset({"PreToolUse", "PostToolUse", "PostToolUseFailure"})
NOT_REPLAYED_CLASSES = frozenset({"http_4xx", "other"})
MAX_PENDING = 500              # records in new/ plus cur/; a new one past this is dropped
RECORD_TTL_S = 86_400          # 24h: a record older than this is dropped unsent
MAX_ATTEMPTS = 5               # redeliveries per record before it is dropped
# Bodies are stored as sent, so one Write of a large file would put that file on
# disk for up to a day, and 500 of them half a gigabyte. A body over this is first
# shrunk (every string longer than SHRINK_STRING_CHARS cut, identity fields kept);
# if it is still over, it is not queued (too_large).
MAX_RECORD_BYTES = 65_536
SHRINK_STRING_CHARS = 1024
SHRINK_KEEP = frozenset({"hook_event_name", "session_id", "tool_use_id", "tool_name",
                         "agent_id", "agent_type", "cwd"})
# Hot-path budget. A drain runs only after this hook's own POST succeeded within
# DRAIN_IF_POSTED_WITHIN_S (a slow API is not handed more work) and spends at most
# DRAIN_BUDGET_S on at most DRAIN_MAX_POSTS redeliveries, each within the remaining
# budget; with less than DRAIN_MIN_POST_S left it stops. The housekeeping before
# the redeliveries (_recover_orphans, _expire) is not cut short by DRAIN_BUDGET_S:
# it runs to the end, and its time is charged to the budget, so it leaves less for
# redeliveries rather than adding to the hook. It is local file work bounded by
# the queue size, at most MAX_PENDING records plus their tmp/ leftovers (500
# expired records took 73ms on the dev machine), and it only has that much to do
# once, after the API was away for longer than RECORD_TTL_S.
DRAIN_IF_POSTED_WITHIN_S = 0.3
DRAIN_BUDGET_S = 0.25
DRAIN_MAX_POSTS = 5
DRAIN_MIN_POST_S = 0.05
# A claim older than this belongs to a hook that died or stalled mid-drain (a live
# drain holds a claim well under a second); its record goes back to new/. The same
# age marks leftovers in tmp/ from a hook that died while writing.
ORPHAN_AFTER_S = 60
REPLAY_RESPONSE_LIMIT = 65536
# Ledger vocabulary for the queue. A failed POST's line says in "spool" whether it
# was queued ("queued", with "shrunk_from" when it had to be cut to fit), why it was
# not by rule (NOT_QUEUED), or that it could not be (spool_full, spool_error,
# too_large). A queue line says in "replay" what happened to a record: delivered,
# duplicate (it had landed after all), failed (requeued), orphan_recovered, or one
# of the drops below, with the record's first failure class as "origin_cls".
# Every eligible event that never reaches the API ends in exactly one of DROPS.
DROPS = ("spool_full", "spool_error", "too_large", "expired", "exhausted", "rejected", "corrupt")
NOT_QUEUED = ("unkeyed", "not_replayable_event", "not_replayable_class")
REPLAY_OUTCOMES = ("delivered", "duplicate", "failed", "orphan_recovered")


def spool_dir() -> str:
    """The replay queue's root (tmp/, new/, cur/ below it)."""
    return os.path.join(ledger_dir(), "spool")


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def _post(data: bytes, api_url: str, timeout: float) -> tuple[str, int | None, object, dict | None]:
    """One POST: (POSTED or failure class, HTTP status, exception, parsed response)."""
    try:
        req = urllib.request.Request(
            f"{api_url}/api/hooks/event",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(REPLAY_RESPONSE_LIMIT)  # Consume response - decisions handled by workflow_reminder.py
        try:
            answer = json.loads(raw) if raw else None
        except ValueError:
            answer = None
        return POSTED, None, None, answer if isinstance(answer, dict) else None
    except Exception as exc:
        cls, status = classify_post_failure(exc)
        return cls, status, exc, None


def post_body(
    data: bytes, api_url: str, event_name: str, *,
    session_id: str = "", tool_use_id: str = "", timeout: float = POST_TIMEOUT,
) -> str:
    """POST one already-serialized hook body. Returns POSTED or a failure class.

    Never raises. A failure writes one stderr line and one ledger line (identifiers
    and timing only, never payload content) and, for a replayable tool event, queues
    the body for redelivery. A quick success drains a few queued records.
    """
    origin_ns = time.time_ns()
    started = time.monotonic()
    cls, status, exc, _ = _post(data, api_url, timeout)
    elapsed = time.monotonic() - started
    if cls == POSTED:
        if elapsed < DRAIN_IF_POSTED_WITHIN_S:
            try:
                drain(api_url)
            except Exception:
                pass
        return POSTED
    session_id = session_id if isinstance(session_id, str) else ""
    tool_use_id = tool_use_id if isinstance(tool_use_id, str) else ""
    sys.stderr.write(
        f"[aiteam-hook] {event_name}: post_failed cls={cls} status={status or '-'} - {exc}\n"
    )
    extra: dict = {}
    if not (session_id and tool_use_id):
        spool = "unkeyed"
    elif event_name not in REPLAYABLE_EVENTS:
        spool = "not_replayable_event"
    elif cls in NOT_REPLAYED_CLASSES:
        spool = "not_replayable_class"
    else:
        spool, extra = _enqueue(data, event_name, session_id, tool_use_id, origin_ns, cls)
    _record({
        "t": _now_iso(),
        "ev": event_name,
        "cls": cls,
        "status": status,
        "ms": int(elapsed * 1000),
        "sid": session_id,
        "keyed": bool(tool_use_id),
        "spool": spool,
        **extra,
    })
    return cls


def _dirs(create: bool = False) -> tuple[str, str, str]:
    root = spool_dir()
    dirs = os.path.join(root, "tmp"), os.path.join(root, "new"), os.path.join(root, "cur")
    if create:
        for directory in dirs:
            os.makedirs(directory, mode=0o700, exist_ok=True)
    return dirs


def _record_name(filename: str) -> str | None:
    """The record a queue file belongs to ("<origin_ns>-<key>.json"), whatever its suffix."""
    head, sep, _ = filename.partition(".json")
    stem = head.split("-", 1)
    if not sep or len(stem) != 2 or not stem[0].isdigit():
        return None
    return head + sep


def _origin_ns(name: str) -> int | None:
    """The origin time a record or claim file name starts with, or None for a foreign file."""
    record = _record_name(name)
    return int(record.split("-", 1)[0]) if record else None


def _token() -> str:
    return f"{os.getpid()}-{os.urandom(4).hex()}"


def _write_new(path: str, record: dict) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, json.dumps(record, separators=(",", ":")).encode("utf-8"))
    finally:
        os.close(fd)


def _retire(path: str, name: str, outcome: str, **fields) -> None:
    """Remove a claimed record from the queue and record the outcome in the ledger."""
    try:
        os.unlink(path)
    except FileNotFoundError:
        return  # someone else already handled it
    except OSError:
        pass
    origin = _origin_ns(name)
    _record({"t": _now_iso(), "replay": outcome,
             "age_s": int((time.time_ns() - origin) / 1e9) if origin else None, **fields})


def _shrink(value: object) -> object:
    """Cut every long string in a payload; dict keys and structure are kept."""
    if isinstance(value, str) and len(value) > SHRINK_STRING_CHARS:
        return value[:SHRINK_STRING_CHARS] + "...(truncated)"
    if isinstance(value, dict):
        return {k: _shrink(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_shrink(v) for v in value]
    return value


def _fit(data: bytes) -> tuple[str | None, int | None]:
    """The body to store (shrunk if it had to be) and its original size, or None if it cannot fit."""
    if len(data) <= MAX_RECORD_BYTES:
        return data.decode("utf-8"), None
    try:
        body = json.loads(data)
    except ValueError:
        return None, len(data)
    if not isinstance(body, dict):
        return None, len(data)
    shrunk = {k: (v if k in SHRINK_KEEP else _shrink(v)) for k, v in body.items()}
    text = json.dumps(shrunk)
    return (text if len(text.encode("utf-8")) <= MAX_RECORD_BYTES else None), len(data)


def _pending(new: str, cur: str) -> list[str]:
    """Record files queued (new/) or claimed (cur/), the count MAX_PENDING and queue_state use."""
    files = []
    for directory in (new, cur):
        try:
            files += [n for n in os.listdir(directory) if _origin_ns(n) is not None]
        except FileNotFoundError:
            continue
    return files


def _enqueue(data: bytes, event_name: str, session_id: str, tool_use_id: str,
             origin_ns: int, origin_cls: str) -> tuple[str, dict]:
    """Queue one failed tool event; returns ("queued" or the drop reason, ledger extras)."""
    try:
        body, original = _fit(data)
        if body is None:
            return "too_large", {"bytes": original}
        tmp, new, cur = _dirs(create=True)
        if len(_pending(new, cur)) >= MAX_PENDING:
            _expire(new, sorted(n for n in os.listdir(new) if _origin_ns(n) is not None))
        if len(_pending(new, cur)) >= MAX_PENDING:
            return "spool_full", {}
        key = hashlib.sha256(
            "\0".join((session_id, event_name, tool_use_id)).encode("utf-8")
        ).hexdigest()[:16]
        name = f"{origin_ns:020d}-{key}.json"
        staged = os.path.join(tmp, f"{name}.{_token()}")
        _write_new(staged, {
            "v": 1, "ev": event_name, "sid": session_id, "tool_use_id": tool_use_id,
            "origin_at": datetime.fromtimestamp(origin_ns / 1e9, UTC).isoformat(),
            "origin_cls": origin_cls, "attempts": 0, "body": body,
        })
        os.replace(staged, os.path.join(new, name))
        return "queued", ({"shrunk_from": original} if original else {})
    except Exception:
        return "spool_error", {}


def _claim(new: str, cur: str, name: str) -> str | None:
    """Move a record from new/ to a cur/ file of its own; None if someone else has it.

    The mtime is refreshed before the move, so the claim never looks like an old
    orphan to a concurrent ``_recover_orphans``.
    """
    source = os.path.join(new, name)
    claimed = os.path.join(cur, f"{name}.{_token()}")
    try:
        os.utime(source)
        os.rename(source, claimed)
    except OSError:
        return None
    return claimed


def _requeue(tmp: str, new: str, claimed: str, name: str, record: dict) -> bool:
    """Put a claimed record back with its new attempt count; False if the claim is gone.

    The claim is first renamed out of cur/ - the one step that fails if a stalled
    hook's claim was recovered meanwhile (then the record is someone else's, and
    putting it back would revive a record that may already be delivered). Only then
    does the updated record go to new/. A crash between the steps leaves both files
    in tmp/, where _recover_orphans finds them.
    """
    token = _token()
    updated = os.path.join(tmp, f"{name}.{token}.new")
    released = os.path.join(tmp, f"{name}.{token}.old")
    _write_new(updated, record)
    try:
        os.rename(claimed, released)
    except FileNotFoundError:
        os.unlink(updated)
        return False
    os.replace(updated, os.path.join(new, name))
    os.unlink(released)
    return True


def _expire(new: str, names: list[str]) -> None:
    """Drop queued records past their TTL (claiming each first, so only one process does)."""
    cutoff = time.time_ns() - RECORD_TTL_S * 1_000_000_000
    cur = os.path.join(os.path.dirname(new), "cur")
    for name in names:
        origin = _origin_ns(name)
        if origin is None or origin >= cutoff:
            continue
        claimed = _claim(new, cur, name)
        if claimed is not None:
            _retire(claimed, name, "expired")


def _recover_orphans(tmp: str, new: str, cur: str) -> None:
    """Put back records a hook left behind when it died or stalled.

    A claim in cur/ older than ORPHAN_AFTER_S goes back to new/. A file in tmp/ that
    old is a record a hook was writing (enqueue) or putting back (requeue) when it
    died: it goes to new/ unless its record is already queued or claimed, in which
    case it is only a leftover and is deleted.
    """
    cutoff = time.time() - ORPHAN_AFTER_S
    for filename in os.listdir(cur):
        name = _record_name(filename)
        if name is None:
            continue
        path = os.path.join(cur, filename)
        try:
            if os.stat(path).st_mtime >= cutoff:
                continue
            os.rename(path, os.path.join(new, name))
        except OSError:
            continue
        _record({"t": _now_iso(), "replay": "orphan_recovered"})
    # ".new" (the updated record) sorts before ".old" (the claim it replaces).
    for filename in sorted(os.listdir(tmp), key=lambda f: f.endswith(".old")):
        name = _record_name(filename)
        if name is None:
            continue
        path = os.path.join(tmp, filename)
        try:
            if os.stat(path).st_mtime >= cutoff:
                continue
            present = os.path.exists(os.path.join(new, name)) or any(
                _record_name(f) == name for f in os.listdir(cur))
            if present:
                os.unlink(path)
                continue
            with open(path, encoding="utf-8") as f:
                json.load(f)  # a half-written file is dropped below, not requeued
            os.rename(path, os.path.join(new, name))
        except ValueError:
            _retire(path, name, "corrupt")
            continue
        except OSError:
            continue
        _record({"t": _now_iso(), "replay": "orphan_recovered"})


def drain(api_url: str, budget_s: float = DRAIN_BUDGET_S) -> int:
    """Redeliver queued records, oldest first, within ``budget_s``. Returns POSTs made.

    Stops early when a redelivery fails, or when the server says the first delivery
    is still being handled: the records wait for a later hook. Never raises out of a
    single record's handling.
    """
    if not os.path.isdir(os.path.join(spool_dir(), "new")):
        return 0
    deadline = time.monotonic() + budget_s
    tmp, new, cur = _dirs(create=True)  # a hand-deleted tmp/ or cur/ comes back
    _recover_orphans(tmp, new, cur)
    names = sorted(n for n in os.listdir(new) if _origin_ns(n) is not None)
    _expire(new, names)
    cutoff = time.time_ns() - RECORD_TTL_S * 1_000_000_000
    posts = 0
    for name in names:
        remaining = deadline - time.monotonic()
        if posts >= DRAIN_MAX_POSTS or remaining < DRAIN_MIN_POST_S:
            break
        if _origin_ns(name) < cutoff:
            continue  # expired above
        claimed = _claim(new, cur, name)
        if claimed is None:
            continue  # another hook claimed it
        try:
            with open(claimed, encoding="utf-8") as f:
                record = json.load(f)
            body = json.loads(record["body"])
            attempt = int(record.get("attempts", 0)) + 1
            if not isinstance(body, dict):
                raise ValueError("body is not an object")
        except Exception:
            _retire(claimed, name, "corrupt")
            continue
        body["_hook_replay"] = {"origin_at": record.get("origin_at"), "attempt": attempt}
        cls, status, _, answer = _post(
            json.dumps(body).encode("utf-8"), api_url, min(POST_TIMEOUT, remaining),
        )
        posts += 1
        fields = {"ev": record.get("ev"), "sid": record.get("sid"), "attempt": attempt,
                  "origin_cls": record.get("origin_cls")}
        in_flight = cls == POSTED and bool(answer) and answer.get("reason") == "in_flight"
        if cls == POSTED and not in_flight:
            duplicate = bool(answer and answer.get("duplicate") is True)
            _retire(claimed, name, "duplicate" if duplicate else "delivered", **fields)
            continue
        if cls in NOT_REPLAYED_CLASSES:
            _retire(claimed, name, "rejected", cls=cls, status=status, **fields)
            continue
        if not in_flight and attempt >= MAX_ATTEMPTS:
            _retire(claimed, name, "exhausted", cls=cls, status=status, **fields)
            break
        # The first delivery still being handled is not a failed attempt: it can
        # still fail and release its receipt, so the record waits, uncounted, until
        # the server answers with that delivery's outcome (a stale claim is taken
        # over after 120s, so this cannot wait forever; RECORD_TTL_S bounds it too).
        if not in_flight:
            record["attempts"] = attempt
        try:
            requeued = _requeue(tmp, new, claimed, name, record)
        except OSError:
            requeued = None  # left claimed: recovered as an orphan after ORPHAN_AFTER_S
        reason = {"reason": "in_flight"} if in_flight else {"cls": cls, "status": status}
        requeue = {True: {}, False: {"requeue": "claim_lost"}, None: {"requeue": "error"}}[requeued]
        _record({"t": _now_iso(), "replay": "failed", **reason, **requeue, **fields})
        break
    return posts


def queue_state() -> dict:
    """Records waiting in the queue and the age of the oldest (for os_health_check)."""
    _, new, cur = _dirs()
    origins = [_origin_ns(n) for n in _pending(new, cur)]
    oldest = min(origins) if origins else None
    return {
        "pending": len(origins),
        "oldest_age_s": int((time.time_ns() - oldest) / 1e9) if oldest else None,
    }
