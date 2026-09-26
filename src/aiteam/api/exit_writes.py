"""Exit-path database writes that give up on a locked database instead of waiting it out.

A timeout that cancels does not bound a write stuck on the SQLite lock. With
SQLAlchemy over aiosqlite the cancel reaches the statement on time, but the
cleanup it triggers (invalidating and closing the connection) is queued on the
connection's worker thread behind the statement that is waiting for the lock, so
the cancelled call returns only when the lock frees, up to the 30s busy timeout.
os_restart_api gives the old process 10s.

So these writes are waited for, not cancelled: past the budget they are left
running and the exit goes on. On the HTTP shutdown path os._exit follows and ends
them; on the lifespan path they finish or fail on their own once the lock frees.
What they would have written is lost, and says so in the log.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable

logger = logging.getLogger(__name__)

EXIT_WRITE_BUDGET_SECONDS = 2.0

# Writes left running past the budget; referenced so they are not collected mid-flight.
_abandoned: set[asyncio.Future] = set()


def _log_late_outcome(task: asyncio.Future) -> None:
    """Retrieve an abandoned write's outcome, so a late failure is logged, not warned about at GC."""
    if task.cancelled():
        return
    if task.exception() is not None:
        logger.debug("Abandoned exit write failed later: %r", task.exception())


async def write_or_abandon(
    writes: dict[str, Awaitable], timeout: float = EXIT_WRITE_BUDGET_SECONDS,
) -> list[str]:
    """Run the writes concurrently for at most ``timeout``; returns the names left running.

    Never raises: a failed write is logged and counted as done.
    """
    tasks = {asyncio.ensure_future(write): name for name, write in writes.items()}
    if not tasks:
        return []
    done, pending = await asyncio.wait(tasks, timeout=timeout)
    for task in done:
        if not task.cancelled() and task.exception() is not None:
            logger.warning("Exit write %s failed: %r", tasks[task], task.exception())
    abandoned = sorted(tasks[task] for task in pending)
    for task in pending:
        _abandoned.add(task)
        task.add_done_callback(_abandoned.discard)
        task.add_done_callback(_log_late_outcome)
    if abandoned:
        logger.warning(
            "Exit writes not done after %.1fs (database locked?), leaving them: %s",
            timeout, ", ".join(abandoned),
        )
    return abandoned
