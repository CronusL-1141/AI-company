"""AI Team OS — Agent activity log routes."""

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
    """Get Agent activity log."""
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
    """Get activity log for all Agents under a session."""
    activities = await repo.list_activities_by_session(session_id, limit=limit)
    return APIListResponse(data=activities, total=len(activities))


@router.get(
    "/api/teams/{team_id}/activities",
    response_model=APIListResponse[AgentActivity],
)
async def list_team_activities(
    team_id: str,
    agent_id: str | None = Query(None, description="Filter by agent (optional)"),
    limit: int = Query(50, ge=1, le=200),
    repo: StorageRepository = Depends(get_repository),
) -> APIListResponse[AgentActivity]:
    """Get team activity log, sorted by timestamp descending, including duration_ms."""
    activities = await repo.list_activities_by_team(team_id, agent_id=agent_id, limit=limit)
    return APIListResponse(data=activities, total=len(activities))
