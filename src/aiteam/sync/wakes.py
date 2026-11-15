"""Scheduled wakes on the center: each runs on the node whose session it wakes.

The center runs the wakes of its own sessions (its reaper, narrowed by origin). Due
wakes of another node's sessions wait until that node claims them, advanced here and
run there; while the node is away they stay due. Rows with no recorded origin
predate the sync: they came with the baseline, copied from the edge's database, so
the edge claims them.
"""

from __future__ import annotations

import asyncio
from datetime import datetime

from aiteam.clock import utc_now
from aiteam.services.scheduling import advance_schedule
from aiteam.storage.repository import StorageRepository
from aiteam.types import ScheduledTask

WAKE = "wake_agent"


class Wakes:
    """Due wakes the center hands to the node they belong to."""

    def __init__(self, repo: StorageRepository, node_id: str) -> None:
        self._repo = repo
        self.node_id = node_id
        self._lock = asyncio.Lock()  # a wake is claimed once

    async def claim(self, node_id: str, now: datetime | None = None) -> list[ScheduledTask]:
        now = now or utc_now()
        async with self._lock:
            due = await self._repo.get_due_tasks(now, origins=(node_id, ""))
            return [task for task in due if task.action_type == WAKE and await advance_schedule(self._repo, task, now)]

    async def awaiting_claim(self, now: datetime | None = None) -> int:
        now = now or utc_now()
        own = {task.id for task in await self._repo.get_due_tasks(now, origins=(self.node_id,))}
        return sum(1 for task in await self._repo.get_due_tasks(now) if task.action_type == WAKE and task.id not in own)
