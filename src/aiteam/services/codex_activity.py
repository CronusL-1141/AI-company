"""Has local Codex written anything since a given moment?

The account monitor samples the native quota and the local session logs. With no
local Codex activity there is nothing new to read, so the round is skipped
(founder's 2026-09-29 ruling on task 551aee38: no new input or output, no read).

Signal: the newest modification time among the session journals Codex appends to
(``$CODEX_HOME/sessions`` and ``archived_sessions``, the same files the capture
reads). A capture at time T already read every line written before T, so a journal
modified after T is exactly "something new to read". Chosen over the usage
recorder's saved facts, which trail the files by up to one discovery sweep
(~75 s for 470 files) and would also flag lines already read by the last capture.

A walk is a few hundred ``stat`` calls, run in a worker thread. Anything it cannot
establish (missing directory, unreadable tree, too many entries) is reported as
unknown, and an unknown answer never skips a round.
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime
from pathlib import Path

from aiteam.clock import from_timestamp

MAX_ENTRIES = 50_000


def codex_home() -> Path:
    """The same root the capture reads (``local_plan_capture._local_source``)."""
    return Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))).expanduser()


def _walk(path: Path, budget: list[int]) -> float:
    newest = 0.0
    with os.scandir(path) as entries:
        for entry in entries:
            budget[0] -= 1
            if budget[0] < 0:
                raise OverflowError("too many journal entries")
            if entry.is_symlink():
                continue
            if entry.is_dir(follow_symlinks=False):
                newest = max(newest, _walk(Path(entry.path), budget))
            elif entry.name.endswith(".jsonl") and entry.is_file(follow_symlinks=False):
                newest = max(newest, entry.stat(follow_symlinks=False).st_mtime)
    return newest


def latest_journal_write(root: Path, *, max_entries: int = MAX_ENTRIES) -> float | None:
    """Newest journal mtime in epoch seconds; 0.0 when there are none; None if unknown."""
    budget = [max_entries]
    newest = 0.0
    found = False
    for name in ("sessions", "archived_sessions"):
        directory = root / name
        if not directory.is_dir():
            continue
        found = True
        try:
            newest = max(newest, _walk(directory, budget))
        except (OSError, OverflowError):
            return None
    return newest if found else None


async def active_since(since: datetime, root: Path | None = None) -> bool | None:
    """True if a journal was written after ``since``, False if not, None if unknown."""
    newest = await asyncio.to_thread(latest_journal_write, root if root is not None else codex_home())
    if newest is None:
        return None
    return newest > 0 and from_timestamp(newest) > since
