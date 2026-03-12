"""AI Team OS — 事件查询路由."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from aiteam.api.deps import get_repository
from aiteam.api.schemas import APIListResponse
from aiteam.storage.repository import StorageRepository
from aiteam.types import Event

router = APIRouter(prefix="/api/events", tags=["events"])


@router.get("", response_model=APIListResponse[Event])
async def list_events(
    type: str | None = Query(None, description="事件类型过滤"),
    source: str | None = Query(None, description="事件来源过滤"),
    limit: int = Query(50, ge=1, le=200, description="返回数量限制"),
    repo: StorageRepository = Depends(get_repository),
) -> APIListResponse[Event]:
    """列出系统事件."""
    events = await repo.list_events(event_type=type, source=source, limit=limit)
    return APIListResponse(data=events, total=len(events))
