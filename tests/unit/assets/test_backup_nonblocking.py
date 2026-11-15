"""The hook response never waits for the backup, and concurrent events record each change once.

Non-blocking is decided by holding the batch work, not by the wall clock: the
batch (read, hash, scan, store) is held on a gate, and the hook's answer must come
back while it is held. Work moved into the request path would answer only after the
gate gives up (GATE_TIMEOUT_SECONDS), with the batch already finished: a sure red.
"""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

from aiteam.assets import ingest
from aiteam.assets.blob_store import BlobStore

from .conftest import PROJECT_SLUG

GATE_TIMEOUT_SECONDS = 10.0
# Concurrent hook requests: enough to queue on the SQLite write lock on a busy machine.
CONCURRENCY = 48
MEMORY = f"projects/{PROJECT_SLUG}/memory"


@pytest.mark.asyncio
async def test_the_hook_answers_while_the_batch_is_held(stack, monkeypatch):
    await stack.enroll()
    gate, entered, finished = threading.Event(), threading.Event(), threading.Event()
    real_run = ingest.run

    def held_run(job):
        entered.set()
        gate.wait(GATE_TIMEOUT_SECONDS)
        try:
            return real_run(job)
        finally:
            finished.set()

    monkeypatch.setattr(ingest, "run", held_run)
    path = stack.folder / MEMORY / "notes.md"
    path.write_text("edited while held\n", encoding="utf-8")
    await stack.hook("PostToolUse", tool_name="Write", tool_use_id="t-held",
                     tool_input={"file_path": str(path), "content": "edited while held"})
    answered_while_held = not finished.is_set()
    # The batch is queued behind the response, then runs on a worker thread.
    await asyncio.to_thread(entered.wait, GATE_TIMEOUT_SECONDS)
    assert entered.is_set() and answered_while_held
    gate.set()
    await stack.settle()
    [content_hash] = [row[0] for row in stack.rows(
        "SELECT content_hash FROM asset_root_state WHERE rel_path = ?", f"{MEMORY}/notes.md")]
    assert BlobStore(stack.data_dir).get(content_hash) == b"edited while held\n"


@pytest.mark.asyncio
async def test_concurrent_writes_and_turn_ends_record_every_change_once(stack):
    await stack.enroll()
    paths = []
    for n in range(CONCURRENCY):
        path = stack.folder / MEMORY / f"burst-{n:02d}.md"
        path.write_text(f"note {n}\n", encoding="utf-8")
        paths.append(path)

    async def write(n: int):
        return await stack.hook("PostToolUse", session_id=f"s-{n % 4}", tool_name="Write", tool_use_id=f"t-{n}",
                                tool_input={"file_path": str(paths[n]), "content": "x"})

    async def turn_end(n: int):
        return await stack.hook("PostToolUse", session_id=f"s-{n}", tool_name="Bash", tool_use_id=f"b-{n}",
                                tool_input={"command": "true"}), await stack.hook("Stop", session_id=f"s-{n}")

    await asyncio.gather(*(write(n) for n in range(CONCURRENCY)), *(turn_end(n) for n in range(4)))
    await stack.settle()
    per_item = stack.rows(
        "SELECT i.rel_path, count(*) FROM asset_versions v JOIN asset_items i ON i.id = v.item_id "
        "WHERE i.rel_path LIKE 'burst-%' GROUP BY i.id")
    assert len(per_item) == CONCURRENCY
    assert {count for _, count in per_item} == {1}
    assert stack.rows("SELECT count(*) FROM asset_root_state WHERE rel_path LIKE ? AND status = 'synced'",
                      f"{MEMORY}/burst-%") == [(CONCURRENCY,)]


# The event loop's longest pause while a 6000-character line is backed up. The scan
# that ran in a thread used to hold the interpreter for about 2s on this input.
LOOP_LAG_CEILING_SECONDS = 0.5


@pytest.mark.asyncio
async def test_a_long_hex_line_does_not_stall_the_event_loop(stack):
    await stack.enroll()
    path = stack.folder / MEMORY / "embedded.md"
    path.write_text("data: " + "0123456789abcdef" * 375 + "\n", encoding="utf-8")
    gaps: list[float] = []
    done = asyncio.Event()

    async def heartbeat() -> None:
        last = time.perf_counter()
        while not done.is_set():
            await asyncio.sleep(0.005)
            now = time.perf_counter()
            gaps.append(now - last)
            last = now

    beat = asyncio.create_task(heartbeat())
    await stack.hook("PostToolUse", tool_name="Write", tool_use_id="t-hex",
                     tool_input={"file_path": str(path), "content": "..."})
    await stack.settle()
    done.set()
    await beat
    assert gaps and max(gaps) < LOOP_LAG_CEILING_SECONDS, max(gaps)
    assert stack.rows("SELECT status FROM asset_root_state WHERE rel_path = ?", f"{MEMORY}/embedded.md") \
        == [("blocked",)]
    assert stack.rows("SELECT rule FROM asset_scan_findings WHERE rel_path = ?", f"{MEMORY}/embedded.md") \
        == [("long_line",)]
