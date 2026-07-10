"""知识层 P1a — 跨域引用图谱查询路由（docs/knowledge-layer-design.md）。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from aiteam.api.deps import get_repository
from aiteam.storage.repository import StorageRepository

router = APIRouter(prefix="/api/links", tags=["knowledge-links"])


@router.get("")
async def list_links(
    kind: str = Query(..., description="端点类型: task_memo/report/task/run/commit/memory"),
    id: str = Query(..., description="端点 ID"),
    direction: str = Query("both", pattern="^(in|out|both)$"),
    limit: int = Query(100, ge=1, le=500),
    repo: StorageRepository = Depends(get_repository),
) -> dict:
    """按端点查引用边。in=谁引用了它 / out=它引用了谁 / both。"""
    links = await repo.find_knowledge_links(kind, id, direction=direction, limit=limit)
    return {"success": True, "data": [lk.model_dump(mode="json") for lk in links]}


@router.get("/fanout")
async def link_fanout(
    kind: str = Query(...),
    id: str = Query(...),
    depth: int = Query(2, ge=1, le=2),
    limit: int = Query(50, ge=1, le=200),
    repo: StorageRepository = Depends(get_repository),
) -> dict:
    """无向递归扇出：从种子节点出发深度 ≤2 的可达节点（回溯链）。"""
    nodes = await repo.knowledge_link_fanout(kind, id, depth=depth, limit=limit)
    return {"success": True, "data": nodes}
