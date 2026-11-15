"""Per-key single-flight background jobs for work a request must not wait on.

The request path only *registers* a job and never waits for it: work that grows
with a file (a transcript parse, a backup pass) would otherwise stall the whole
process for every session queued behind it, and uvicorn silently drops requests
whose client gave up meanwhile.

Semantics:

* one task per key at a time; a submit while one runs only marks the key dirty,
  and the task runs **exactly one more round** afterwards (with the last job
  submitted), however many submits came in. A queued ``sticky`` job (a final
  measurement that includes the ordinary one) is only replaced by another sticky job;
* tasks are held by strong references until they finish: the event loop keeps
  only weak ones, and a bare ``create_task`` may be collected mid-run;
* every task ends (event-triggered, runs to completion), none is a resident
  consumer loop: the system deliberately has no background daemon.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping

logger = logging.getLogger(__name__)

BackgroundJob = Callable[[], Awaitable[None]]
JobKey = tuple[str, str]


class KeyedSingleFlight:
    """Run at most one job per key, coalescing submits into one trailing rerun.

    ``recovery`` maps a job kind (the first part of the key) to how its work comes
    back when a drain cancels it; the drain warning quotes it. ``label`` names the
    owner in that warning.
    """

    def __init__(self, *, recovery: Mapping[str, str] | None = None, label: str = "hook background") -> None:
        self._running: dict[JobKey, asyncio.Task[None]] = {}
        self._rerun: dict[JobKey, tuple[BackgroundJob, bool]] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        self._recovery = recovery or {}
        self._label = label

    def submit(self, key: JobKey, job: BackgroundJob, *, sticky: bool = False) -> bool:
        """Start ``job`` for ``key`` now, or mark the key dirty if one is in flight.

        A queued ``sticky`` job is only replaced by another sticky job.
        Returns True when a new task was started, False when coalesced.
        """
        if key in self._running:
            queued = self._rerun.get(key)
            if sticky or queued is None or not queued[1]:
                self._rerun[key] = (job, sticky)
            return False
        task = asyncio.get_running_loop().create_task(self._run(key, job), name=f"{self._label}:{key[0]}")
        self._running[key] = task
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return True

    async def _run(self, key: JobKey, job: BackgroundJob) -> None:
        try:
            while True:
                try:
                    await job()
                except Exception:  # noqa: BLE001 - jobs log their own failures; this keeps the rerun alive
                    logger.warning("%s job %s failed", self._label, key[0], exc_info=True)
                # No await between taking and checking the rerun: a submit cannot slip in.
                queued = self._rerun.pop(key, None)
                if queued is None:
                    return
                job = queued[0]
        finally:
            self._running.pop(key, None)
            self._rerun.pop(key, None)

    @property
    def in_flight(self) -> int:
        return len(self._tasks)

    async def drain(self, timeout: float | None = None, *, leave_leftovers: bool = False) -> bool:
        """Wait for in-flight jobs (including their pending rerun); at ``timeout`` deal with leftovers.

        Returns True when everything finished. Two ways to end, one per exit path:

        * default (lifespan, ``close_db`` follows): cancel the leftovers and wait for
          them, so nothing writes after the engine is gone. Not bounded by ``timeout``
          when a job is stuck on the SQLite write lock: the cancel's cleanup (closing
          the connection) queues behind the statement waiting for the lock. A
          cancelled job waiting on ``asyncio.to_thread`` leaves its thread to finish
          unclaimed; it does not write, so nothing lands after ``close_db``.
        * ``leave_leftovers`` (HTTP shutdown, ``os._exit`` follows): stop waiting and
          leave them running, never cancel; returns at ``timeout`` even on a locked
          database. The hard exit ends them; what they would have written is lost the
          same way a cancelled job's is, and is logged the same way.
        """
        loop = asyncio.get_running_loop()
        deadline = None if timeout is None else loop.time() + timeout
        while self._tasks:
            remaining = None if deadline is None else deadline - loop.time()
            if remaining is not None and remaining <= 0:
                break
            await asyncio.wait(set(self._tasks), timeout=remaining)
        if not self._tasks:
            return True
        leftover = list(self._tasks)
        # Keys before cancelling (finished tasks leave _running, only the cancelled stay).
        cancelled = sorted(key for key, task in self._running.items() if task in leftover)
        if not leave_leftovers:
            for task in leftover:
                task.cancel()
            await asyncio.gather(*leftover, return_exceptions=True)
        kinds = dict.fromkeys(kind for kind, _ in cancelled)
        logger.warning(
            "%s drain: %d job(s) %s after %.1fs: %s. Recovery: %s",
            self._label,
            len(leftover),
            "left unfinished at exit" if leave_leftovers else "cancelled",
            timeout or 0.0,
            ", ".join(f"{kind}:{ident}" for kind, ident in cancelled) or "unknown",
            "; ".join(
                f"{kind} {self._recovery.get(kind, 'has no recorded recovery path')}"
                for kind in kinds
            ) or "none recorded",
        )
        return False
