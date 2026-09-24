"""AI Team OS — Leader Briefing routes."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from aiteam.api.deps import get_event_bus, get_repository, get_scoped_repository
from aiteam.api.event_bus import EventBus
from aiteam.api.language import resolve_language
from aiteam.clock import utc_now
from aiteam.services.notices import ledger
from aiteam.services.notices.detectors.decisions import expire_stale, is_real_pending
from aiteam.storage.repository import StorageRepository
from aiteam.types import EventType

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/leader-briefings", tags=["briefings"])


class BriefingCreateBody(BaseModel):
    title: str
    description: str = ""
    options: str = ""
    recommendation: str = ""
    urgency: str = "medium"
    project_id: str = ""
    tags: list[str] = Field(default_factory=list)


# Returned by briefing_add when a user prompt arrived in this project recently.
_PRESENT_HINT = {
    "zh": (
        "用户在场，请直接问：本项目 {minutes} 分钟前还有用户消息。能在对话里问的就直接问，"
        "用户答复后对这一条调用 briefing_resolve 记下答复；不需要了就用 briefing_dismiss 撤掉。"
        "待决事项只留给用户不在场时攒下的问题（自主推进、后台任务、其他会话）。"
    ),
    "en": (
        "The user is present, ask directly: this project had a user message {minutes} minute(s) ago. "
        "Ask in the conversation, then record the answer with briefing_resolve on this item, or "
        "remove it with briefing_dismiss if it is no longer needed. Pending decisions are only for "
        "questions collected while the user is away (autonomous work, background tasks, other sessions)."
    ),
}


class BriefingResolveBody(BaseModel):
    resolution: str


@router.get("")
async def list_briefings(
    status: str = "pending",
    project_id: str = "",
    tag: str = "",
    real_only: bool = False,
    repo: StorageRepository = Depends(get_scoped_repository),
) -> dict[str, Any]:
    """List leader briefing items filtered by status, project and tag.

    Pending items older than 14 days are moved to ``expired`` first (status
    only, rows kept). ``real_only`` drops automatic permission-denial records,
    the same rule the pending-decisions notice uses.
    """
    await expire_stale(repo, utc_now())
    items = await repo.list_briefings(status=status, project_id=project_id, tag=tag)
    if real_only:
        items = [item for item in items if item.status != "pending" or is_real_pending(item)]
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
        tags=body.tags,
    )
    result = briefing.model_dump(mode="json")
    seen = ledger.user_recently_active(repo, body.project_id)
    if seen is not None:
        minutes = max(0, int((utc_now() - seen).total_seconds() // 60))
        language = (await resolve_language(host="cc"))["effective"]
        result["user_present"] = True
        result["hint"] = _PRESENT_HINT[language].format(minutes=minutes)
    return result


@router.put("/{briefing_id}/resolve")
async def resolve_briefing(
    briefing_id: str,
    body: BriefingResolveBody,
    repo: StorageRepository = Depends(get_repository),
    event_bus: EventBus = Depends(get_event_bus),
) -> dict[str, Any]:
    """Resolve a briefing item with the user's decision and record it as a decision event."""
    result = await repo.resolve_briefing(briefing_id, resolution=body.resolution)
    if result is None:
        raise HTTPException(status_code=404, detail="Briefing not found")
    try:
        await event_bus.emit(
            event_type=EventType.DECISION_BRIEFING_RESOLVED.value,
            source=f"briefing:{result.id}",
            data={
                "briefing_id": result.id,
                "title": result.title,
                "resolution": result.resolution,
                "project_id": result.project_id,
                "tags": result.tags,
            },
            entity_id=result.id,
            entity_type="briefing",
        )
    except Exception:  # noqa: BLE001 - the resolution itself is already stored
        logger.warning("decision event for briefing %s failed", briefing_id, exc_info=True)
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
