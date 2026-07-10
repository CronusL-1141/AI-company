"""知识层 P1b — 统一检索路由。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from aiteam.api.deps import get_repository
from aiteam.api.unified_search import unified_search
from aiteam.storage.repository import StorageRepository

router = APIRouter(prefix="/api/search", tags=["unified-search"])


@router.get("")
async def search(
    q: str = Query(..., min_length=1, max_length=200),
    limit: int = Query(10, ge=1, le=50),
    project_id: str = Query("", description="限定项目（空=全部）"),
    repo: StorageRepository = Depends(get_repository),
) -> dict:
    """三臂 RRF 统一检索：memo/report/task 全文 + 引用图谱 + 精确 ID/标题。"""
    results = await unified_search(
        repo, q, limit=limit, project_id=project_id or None
    )
    return {"success": True, "data": results}
