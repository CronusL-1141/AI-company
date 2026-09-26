"""API exits must not wait out a locked database: HTTP shutdown with a hook job stuck on the lock, and SIGTERM.

End to end: a real API process. A SessionEnd hook schedules the leader-usage
capture as a background job (transcript parse, then a DB write). An outside
connection takes the write lock while the job parses, so its write blocks. Then
POST /api/system/shutdown, the path os_restart_api uses. The old drain waited its
3s and then cancelled the job and gathered it; the cancel's cleanup waits for the
lock too (SQLAlchemy closes the connection behind the blocked statement), so the
process stayed up until the lock was released and the restart gave up at 10s.
"""

from __future__ import annotations

import json
import signal
import sqlite3
import subprocess
import time
import urllib.request
import uuid
from pathlib import Path

from aiteam.api.exit_writes import EXIT_WRITE_BUDGET_SECONDS
from aiteam.api.hook_translator import BACKGROUND_DRAIN_TIMEOUT_SECONDS
from aiteam.api.routes import system
from tests.integration.test_hook_ingest_counts_restart import (
    RESTART_EXIT_BUDGET,
    _Api,
    _free_port,
)

# Parsing this takes a fraction of a second: long enough for the test to take the
# lock before the job's write, short enough to end well before the drain budget.
TRANSCRIPT_REQUESTS = 150_000


def _post(api: _Api, payload: dict, path: str = "/api/hooks/event") -> dict:
    request = urllib.request.Request(
        f"{api.url}{path}", data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read())


def _write_transcript(path: Path) -> None:
    row = {"type": "assistant", "requestId": "", "message": {
        "model": "claude-opus-5", "content": [{"type": "text", "text": "x" * 200}],
        "usage": {"input_tokens": 1, "output_tokens": 2,
                  "cache_creation_input_tokens": 3, "cache_read_input_tokens": 4},
    }}
    with path.open("w", encoding="utf-8") as handle:
        for index in range(TRANSCRIPT_REQUESTS):
            row["requestId"] = f"req_{index}"
            handle.write(json.dumps(row) + "\n")


def test_http_shutdown_leaves_a_locked_background_job_and_exits_in_budget(tmp_path):
    api = _Api(tmp_path, _free_port())
    session = str(uuid.uuid4())
    cwd = str(tmp_path / "work")
    Path(cwd).mkdir()
    started_at = None
    locker = None
    try:
        # A registered project for the cwd, so SessionStart creates the session's leader.
        _post(api, {"name": "exit-bounds", "root_path": cwd}, path="/api/projects")
        _post(api, {"hook_event_name": "SessionStart", "session_id": session, "cwd": cwd})
        transcript = tmp_path / f"{session}.jsonl"
        _write_transcript(transcript)
        end = _post(api, {
            "hook_event_name": "SessionEnd", "session_id": session, "cwd": cwd,
            "transcript_path": str(transcript), "reason": "exit",
        })
        # The capture was handed to the background, not done in the response.
        assert "deferred" in json.dumps(end), end
        locker = sqlite3.connect(api.env["AITEAM_DB_PATH"], isolation_level=None, timeout=10)
        locker.execute("BEGIN IMMEDIATE")
        request = urllib.request.Request(f"{api.url}/api/system/shutdown", method="POST")
        started_at = time.monotonic()
        with urllib.request.urlopen(request, timeout=5):
            pass
        try:
            api.proc.wait(RESTART_EXIT_BUDGET + 5)
        except subprocess.TimeoutExpired:
            pass
        exited_after = time.monotonic() - started_at if api.proc.poll() is not None else None
    finally:
        if locker is not None:
            if started_at is not None:
                time.sleep(max(0.0, RESTART_EXIT_BUDGET + 2 - (time.monotonic() - started_at)))
            locker.execute("ROLLBACK")
            locker.close()
        api.wait_exit()
    log = (Path(api.env["HOME"]) / ".claude" / "data" / "ai-team-os" / "debug.log").read_text(
        encoding="utf-8", errors="replace",
    )
    print(f"http shutdown with a job stuck on the lock: exited after {exited_after}s")
    # The scenario really had the job in flight at shutdown, blocked on the lock.
    assert "job(s) left unfinished at exit" in log and "leader-usage:" in log, log[-3000:]
    # Response flush, the drain's wait, the exit-write budget, diagnostics; the
    # checkpoint is skipped because the exit writes were left. One second of slack.
    bound = (
        system.RESPONSE_FLUSH_SECONDS + BACKGROUND_DRAIN_TIMEOUT_SECONDS
        + EXIT_WRITE_BUDGET_SECONDS + system.DIAGNOSTICS_FLUSH_SECONDS + 1.0
    )
    assert bound < RESTART_EXIT_BUDGET
    assert exited_after is not None and exited_after < bound, (exited_after, bound)
    assert api.proc.returncode == 0


def test_sigterm_exit_does_not_wait_out_a_locked_database(tmp_path):
    """The lifespan path (SIGTERM) has no outside budget, but it must not hang either.

    StateReaper.stop hands the governance lease back; that write used to wait for
    the lock for the whole 30s busy timeout. It is now bounded at the lock wait.
    """
    api = _Api(tmp_path, _free_port())
    locker = sqlite3.connect(api.env["AITEAM_DB_PATH"], isolation_level=None, timeout=10)
    try:
        locker.execute("BEGIN IMMEDIATE")
        started_at = time.monotonic()
        api.proc.send_signal(signal.SIGTERM)
        try:
            api.proc.wait(RESTART_EXIT_BUDGET + 5)
        except subprocess.TimeoutExpired:
            pass
        exited_after = time.monotonic() - started_at if api.proc.poll() is not None else None
    finally:
        locker.execute("ROLLBACK")
        locker.close()
        api.wait_exit()
    print(f"sigterm under a held lock: exited after {exited_after}s")
    assert exited_after is not None and exited_after < RESTART_EXIT_BUDGET - 1, exited_after
