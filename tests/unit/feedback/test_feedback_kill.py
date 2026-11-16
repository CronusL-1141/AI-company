"""Fault injection: the API process is killed while gh is publishing (13.3 of the design).

The child process is a real one running the production service; gh (fake_gh.py)
creates the issue and then hangs before answering, and the test kills both. A new
app instance on the same database must not leave the row sending, must find the
post by its marker and fill it in, and must never post a second copy.
"""

from __future__ import annotations

import os
import select
import signal
import subprocess
import sys
from pathlib import Path

from .conftest import BUG, REPO, close_stack, open_stack

CHILD = Path(__file__).with_name("submit_child.py")
SRC = Path(__file__).resolve().parents[3] / "src"


def _read_line(fifo: Path, timeout: float) -> str:
    """One line from ``fifo``; opened read-write so there is always a writer and select waits for data."""
    fd = os.open(fifo, os.O_RDWR | os.O_NONBLOCK)
    try:
        ready, _, _ = select.select([fd], [], [], timeout)
        assert ready, "the publish never reached gh"
        return os.read(fd, 4096).decode().strip()
    finally:
        os.close(fd)


async def test_killed_while_sending_then_restarted_the_post_is_found_not_repeated(tmp_path, monkeypatch,
                                                                                  fake_gh) -> None:
    stack = await open_stack(tmp_path, monkeypatch, fake_gh)
    try:
        draft = await stack.draft(**BUG)
    finally:
        await close_stack(stack)
    fifo = tmp_path / "gh-signal"
    os.mkfifo(fifo)
    fake_gh.set(hang={"create_issue": "after"}, signal=str(fifo))
    child = subprocess.Popen(
        [sys.executable, str(CHILD), str(stack.database), str(stack.data_dir), draft["draft_id"],
         draft["content_hash"], REPO],
        env={**os.environ, "PYTHONPATH": os.pathsep.join([str(SRC), os.environ.get("PYTHONPATH", "")])})
    try:
        gh_pid, op = _read_line(fifo, timeout=60).split()
        assert op == "create_issue"
    finally:
        child.kill()
        child.wait(30)
    os.kill(int(gh_pid), signal.SIGKILL)
    assert len(fake_gh.repo()["issues"]) == 1  # it did reach GitHub
    assert stack.ledger(draft["draft_id"])["status"] == "sending"  # and the API never heard back

    restarted = await open_stack(tmp_path, monkeypatch, fake_gh, database=stack.database)
    try:
        fake_gh.set(hang={})
        restarted.clock.advance(minutes=1)  # past the time a send may honestly take
        (row,) = (await restarted.client.get("/api/feedback")).json()["data"]
        (issue,) = fake_gh.repo()["issues"]
        assert (row["status"], row["remote_number"], row["remote_url"]) == ("sent", issue["number"],
                                                                            issue["html_url"])
        assert (await restarted.draft(**BUG))["remote_url"] == issue["html_url"]
        response = await restarted.submit(draft["draft_id"], draft["content_hash"])
        assert response.json() | {"already": True} == response.json() and response.json()["status"] == "sent"
        assert len(fake_gh.repo()["issues"]) == 1 and len(fake_gh.calls("create_issue")) == 1
        (event,) = restarted.events("feedback.submitted")
        assert event["reconciled"] is True and event["drafted_in_session"] == "session-1"
    finally:
        await close_stack(restarted)
