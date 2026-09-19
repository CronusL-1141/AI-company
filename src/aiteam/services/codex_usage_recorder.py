"""API-lifetime local usage recording, independent of account prediction."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import deque
from pathlib import Path

from aiteam.services.codex_usage_journal import (
    CodexJournalDiscovery,
    probe_source,
    scan_journal_file,
    source_namespace,
)
from aiteam.storage.codex_usage_journal import CodexUsageJournalRepository

_logger = logging.getLogger(__name__)


class CodexUsageRecorder:
    """Incrementally save local facts without native requests or account lookup."""

    def __init__(
        self, repository: CodexUsageJournalRepository, *, codex_home: Path | None = None,
        interval_seconds: float = 5, max_scan_seconds: float = 1,
        max_scan_bytes: int = 8 * 1024 * 1024, max_files: int = 32,
    ) -> None:
        self._repository = repository
        self._home = codex_home if codex_home is not None else Path(
            os.environ.get("CODEX_HOME") or Path.home() / ".codex",
        )
        self._namespace = source_namespace(self._home)
        self._discovery = CodexJournalDiscovery(self._home)
        self._pending: deque[Path] = deque()
        self._interval = max(0.01, interval_seconds)
        self._max_seconds = max(0.01, max_scan_seconds)
        self._max_bytes = max(32768, max_scan_bytes)
        self._max_files = max(1, max_files)
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._lifecycle_lock = asyncio.Lock()
        self._tick_lock = asyncio.Lock()
        self.last_result: dict[str, int | str] = {}
        self.last_error: str | None = None

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done() and not self._stop.is_set()

    @staticmethod
    async def _thread(function, *args, **kwargs):
        """Do not leave a file-reading worker running after cancellation."""
        task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            if not task.cancelled():
                task.exception()
            raise

    async def start(self) -> None:
        async with self._lifecycle_lock:
            if self.is_running:
                return
            await self._repository.init_db()
            self._stop.clear()
            self._task = asyncio.create_task(self._run(), name="codex-usage-recorder")

    async def stop(self) -> None:
        async with self._lifecycle_lock:
            self._stop.set()
            if self._task is not None:
                self._task.cancel()
                try:
                    await self._task
                except asyncio.CancelledError:
                    pass
                self._task = None
            async with self._tick_lock:
                await self._thread(self._discovery.close)

    async def run_once(self) -> dict[str, int | str]:
        """Perform a bounded fair pass; database arbitration protects other workers."""
        async with self._tick_lock:
            deadline = time.monotonic() + self._max_seconds
            result: dict[str, int | str] = {
                "files_checked": 0, "bytes_read": 0, "committed_batches": 0,
                "observations_seen": 0, "cursor_conflicts": 0, "source_errors": 0,
                "status": "scan_complete",
            }
            if not self._pending:
                try:
                    self._pending.extend(await self._thread(
                        self._discovery.next_paths, max_files=self._max_files,
                        max_entries=1024, deadline=deadline,
                    ))
                    if self._discovery.budget_exhausted:
                        result["status"] = "scan_limited"
                except (OSError, ValueError, RecursionError):
                    result["source_errors"] += 1
                    await self._thread(self._discovery.close)
            for _ in range(self._max_files):
                if not self._pending:
                    break
                remaining = self._max_bytes - result["bytes_read"]
                if time.monotonic() >= deadline or remaining <= 32768:
                    result["status"] = "scan_limited"
                    break
                path = self._pending.popleft()
                try:
                    source_id = await self._thread(probe_source, path, self._namespace)
                    previous = await self._repository.get_cursor(source_id)
                    batch = await self._thread(
                        scan_journal_file, path, self._namespace, previous,
                        max_bytes=min(2 * 1024 * 1024 + 32768, remaining), deadline=deadline,
                    )
                    result["files_checked"] += 1
                    result["bytes_read"] += batch.bytes_read
                    if await self._repository.commit_batch(batch):
                        result["committed_batches"] += 1
                        result["observations_seen"] += len(batch.observations)
                    else:
                        result["cursor_conflicts"] += 1
                    if batch.cursor.status == "scan_limited":
                        result["status"] = "scan_limited"
                except (OSError, ValueError, RecursionError):
                    result["source_errors"] += 1
            self.last_result = result
            return result

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await self.run_once()
                self.last_error = None
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self.last_error = type(error).__name__
                _logger.warning("Codex usage recorder failed (%s)", self.last_error)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval)
            except TimeoutError:
                pass
