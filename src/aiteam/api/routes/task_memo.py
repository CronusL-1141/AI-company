"""AI Team OS — Task memo tracking routes.

Provides task memo read and append functionality for recording task progress, decisions, issues, and summaries.
Memos are stored in Task.config["memo"], no new database table needed.
"""

from __future__ import annotations

import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException

from aiteam.api.deps import get_repository
from aiteam.api.schemas import MemoEntry
from aiteam.storage.repository import StorageRepository

logger = logging.getLogger(__name__)

router = APIRouter(tags=["task-memo"])


@router.get("/api/tasks/{task_id}/memo")
async def get_task_memo(
    task_id: str,
    repo: StorageRepository = Depends(get_repository),
) -> dict:
    """Get task memo record list."""
    task = await repo.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"任务 {task_id} 不存在")
    memos = task.config.get("memo", [])
    return {"success": True, "data": memos}


@router.post("/api/tasks/{task_id}/memo")
async def add_task_memo(
    task_id: str,
    body: MemoEntry,
    repo: StorageRepository = Depends(get_repository),
) -> dict:
    """Append a memo record."""
    task = await repo.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"任务 {task_id} 不存在")

    config = dict(task.config) if task.config else {}
    memos = list(config.get("memo", []))
    entry = {
        "timestamp": datetime.now().isoformat(),
        "author": body.author,
        "content": body.content,
        "type": body.type,
    }
    memos.append(entry)
    config["memo"] = memos
    await repo.update_task(task_id, config=config)

    return {"success": True, "data": entry}
