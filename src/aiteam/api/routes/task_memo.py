"""AI Team OS — 任务Memo追踪路由.

提供任务memo的读取和追加功能，用于记录任务进度、决策、问题和总结。
Memo存储在Task.config["memo"]中，无需新建数据库表。
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
    """获取任务的memo记录列表."""
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
    """追加一条memo记录."""
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
