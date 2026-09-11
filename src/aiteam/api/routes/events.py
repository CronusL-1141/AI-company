"""AI Team OS — Event query routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from aiteam.api.deps import get_scoped_repository
from aiteam.api.schemas import APIListResponse
from aiteam.storage.repository import StorageRepository
from aiteam.types import Event

router = APIRouter(prefix="/api/events", tags=["events"])


@router.get("", response_model=APIListResponse[Event])
async def list_events(
    type: str | None = Query(None, description="Event type filter"),
    type_prefix: str | None = Query(None, description="Literal event type prefix; type takes precedence"),
    source: str | None = Query(None, description="Event source filter"),
    entity_id: str | None = Query(None, description="Filter by entity ID (task/agent/team/meeting)"),
    limit: int = Query(50, ge=1, le=200, description="Return count limit"),
    project_id: str = Query("", description="Filter events by project (including teamless tasks)"),
    repo: StorageRepository = Depends(get_scoped_repository),
) -> APIListResponse[Event]:
    """List system events, optionally filtered by type, source, entity_id, or project."""
    events = await repo.list_events(
        event_type=type,
        type_prefix=type_prefix,
        source=source,
        entity_id=entity_id,
        limit=limit,
        project_id=project_id or None,
    )
    return APIListResponse(data=events, total=len(events))
