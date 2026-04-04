"""AI Team OS — Leader Briefing routes."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from aiteam.api.deps import get_repository, get_scoped_repository
from aiteam.storage.repository import StorageRepository

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/leader-briefings", tags=["briefings"])


class BriefingCreateBody(BaseModel):
    title: str
    description: str = ""
    options: str = ""
    recommendation: str = ""
    urgency: str = "medium"
    project_id: str = ""


class BriefingResolveBody(BaseModel):
    resolution: str


@router.get("")
async def list_briefings(
    status: str = "pending",
    project_id: str = "",
    repo: StorageRepository = Depends(get_scoped_repository),
) -> dict[str, Any]:
    """List leader briefing items filtered by status."""
    items = await repo.list_briefings(status=status, project_id=project_id)
    return {"items": [i.model_dump(mode="json") for i in items], "total": len(items)}


@router.post("")
async def create_briefing(
    body: BriefingCreateBody,
    repo: StorageRepository = Depends(get_repository),
) -> dict[str, Any]:
    """Create a new leader briefing item."""
    valid_urgencies = ("high", "medium", "low")
    if body.urgency not in valid_urgencies:
        raise HTTPException(
            status_code=400,
            detail=f"urgency must be one of: {valid_urgencies}",
        )
    briefing = await repo.create_briefing(
        title=body.title,
        description=body.description,
        options=body.options,
        recommendation=body.recommendation,
        urgency=body.urgency,
        project_id=body.project_id,
    )
    return briefing.model_dump(mode="json")


@router.put("/{briefing_id}/resolve")
async def resolve_briefing(
    briefing_id: str,
    body: BriefingResolveBody,
    repo: StorageRepository = Depends(get_repository),
) -> dict[str, Any]:
    """Resolve a briefing item with user's decision."""
    result = await repo.resolve_briefing(briefing_id, resolution=body.resolution)
    if result is None:
        raise HTTPException(status_code=404, detail="Briefing not found")
    return result.model_dump(mode="json")


@router.put("/{briefing_id}/dismiss")
async def dismiss_briefing(
    briefing_id: str,
    repo: StorageRepository = Depends(get_repository),
) -> dict[str, Any]:
    """Dismiss a briefing item without a resolution."""
    result = await repo.dismiss_briefing(briefing_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Briefing not found")
    return result.model_dump(mode="json")
