"""Record synthetic native Codex logs through the production journal path."""

from __future__ import annotations

from pathlib import Path

from aiteam.services.codex_usage_journal import probe_source, scan_journal_file, source_namespace
from aiteam.storage.codex_usage_journal import CodexUsageJournalRepository


async def record_all(repository: CodexUsageJournalRepository, home: Path, *, max_bytes: int | None = None) -> int:
    """Bring every log under ``home`` up to date, as the recorder does over its ticks.

    Sessions are recorded before archives, each in path order. Returns the
    number of committed scans (a small ``max_bytes`` needs several per file).
    """
    namespace = source_namespace(home)
    options = {} if max_bytes is None else {"max_bytes": max_bytes}
    scans = 0
    for area in ("sessions", "archived_sessions"):
        folder = home / area
        if not folder.is_dir():
            continue
        for path in sorted(folder.rglob("*.jsonl")):
            previous = await repository.get_cursor(probe_source(path, namespace))
            while True:
                batch = scan_journal_file(path, namespace, previous, **options)
                assert await repository.commit_batch(batch)
                scans += 1
                previous = batch.cursor
                if batch.cursor.status != "scan_limited":
                    break
    return scans
