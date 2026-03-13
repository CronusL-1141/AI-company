"""AI Team OS — Agent活动日志路由."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from aiteam.api.deps import get_repository
from aiteam.api.schemas import APIListResponse
from aiteam.storage.repository import StorageRepository
from aiteam.types import AgentActivity

router = APIRouter(tags=["activities"])


@router.get(
    "/api/agents/{agent_id}/activities",
    response_model=APIListResponse[AgentActivity],
)
async def list_agent_activities(
    agent_id: str,
    limit: int = Query(50, ge=1, le=200),
    repo: StorageRepository = Depends(get_repository),
) -> APIListResponse[AgentActivity]:
    """获取Agent的活动日志."""
    activities = await repo.list_activities(agent_id, limit=limit)
    return APIListResponse(data=activities, total=len(activities))


@router.get(
    "/api/sessions/{session_id}/activities",
    response_model=APIListResponse[AgentActivity],
)
async def list_session_activities(
    session_id: str,
    limit: int = Query(100, ge=1, le=500),
    repo: StorageRepository = Depends(get_repository),
) -> APIListResponse[AgentActivity]:
    """获取某session下所有Agent的活动日志."""
    activities = await repo.list_activities_by_session(session_id, limit=limit)
    return APIListResponse(data=activities, total=len(activities))
