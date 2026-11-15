"""Moving scheduled tasks from one run to the next.

One rule for every node that runs scheduled wakes: a node running its own, and a
center handing another node's due wakes over to it (aiteam.sync.wakes).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from aiteam.storage.repository import StorageRepository
from aiteam.types import ScheduledTask

logger = logging.getLogger(__name__)

# A run missed by more than this is skipped, not caught up: missed runs never pile up.
MISSED_AFTER = timedelta(hours=1)


async def advance_schedule(repo: StorageRepository, task: ScheduledTask, now: datetime) -> bool:
    """Move a due scheduled task to its next run; False when this run was missed."""
    overdue = now - task.next_run_at
    await repo.update_scheduled_task(
        task.id, last_run_at=now, next_run_at=now + timedelta(seconds=task.interval_seconds))
    if overdue > MISSED_AFTER:
        logger.info("Scheduled task '%s' is past-due by %.0f min, skipping", task.name, overdue.total_seconds() / 60)
        return False
    return True
