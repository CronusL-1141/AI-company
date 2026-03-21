"""AI Team OS — Event query routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from aiteam.api.deps import get_repository
from aiteam.api.schemas import APIListResponse
from aiteam.storage.repository import StorageRepository
from aiteam.types import Event

router = APIRouter(prefix="/api/events", tags=["events"])


@router.get("", response_model=APIListResponse[Event])
async def list_events(
    type: str | None = Query(None, description="Event type filter"),
    source: str | None = Query(None, description="Event source filter"),
    limit: int = Query(50, ge=1, le=200, description="Return count limit"),
    repo: StorageRepository = Depends(get_repository),
) -> APIListResponse[Event]:
    """List system events."""
    events = await repo.list_events(event_type=type, source=source, limit=limit)
    return APIListResponse(data=events, total=len(events))
