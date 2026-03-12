"""AI Team OS — 记忆查询路由."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from aiteam.api.deps import get_repository
from aiteam.api.schemas import APIListResponse
from aiteam.storage.repository import StorageRepository
from aiteam.types import Memory

router = APIRouter(prefix="/api/memory", tags=["memory"])


@router.get("", response_model=APIListResponse[Memory])
async def search_memories(
    scope: str = Query("global", description="记忆作用域"),
    scope_id: str = Query("system", description="作用域ID"),
    query: str = Query("", description="搜索关键词"),
    limit: int = Query(10, ge=1, le=100, description="返回数量限制"),
    repo: StorageRepository = Depends(get_repository),
) -> APIListResponse[Memory]:
    """搜索记忆."""
    if query:
        memories = await repo.search_memories(scope, scope_id, query, limit)
    else:
        memories = await repo.list_memories(scope, scope_id)
        memories = memories[:limit]
    return APIListResponse(data=memories, total=len(memories))
