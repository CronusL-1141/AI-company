"""AI Team OS — Memory query routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from aiteam.api.deps import get_repository
from aiteam.api.schemas import APIListResponse
from aiteam.storage.repository import StorageRepository
from aiteam.types import Memory

router = APIRouter(prefix="/api/memory", tags=["memory"])


@router.get("", response_model=APIListResponse[Memory])
async def search_memories(
    scope: str = Query("global", description="Memory scope"),
    scope_id: str = Query("system", description="Scope ID"),
    query: str = Query("", description="Search keywords"),
    limit: int = Query(10, ge=1, le=100, description="Return count limit"),
    repo: StorageRepository = Depends(get_repository),
) -> APIListResponse[Memory]:
    """Search memories."""
    if query:
        memories = await repo.search_memories(scope, scope_id, query, limit)
    else:
        memories = await repo.list_memories(scope, scope_id)
        memories = memories[:limit]
    return APIListResponse(data=memories, total=len(memories))


# ================================================================
# Team knowledge base endpoint
# ================================================================

router_teams_memory = APIRouter(prefix="/api/teams", tags=["memory"])


@router_teams_memory.get("/{team_id}/knowledge", response_model=APIListResponse[Memory])
async def get_team_knowledge(
    team_id: str,
    type: str = Query("", description="Type filter: failure_alchemy / lesson_learned / loop_review"),
    limit: int = Query(50, ge=1, le=200, description="Return count limit"),
    repo: StorageRepository = Depends(get_repository),
) -> APIListResponse[Memory]:
    """Get team knowledge base.

    Returns the team's scope=team memory list, including:
    - failure_alchemy generated failure lessons
    - lesson_learned manually recorded experiences
    - loop_review retrospective summaries
    Sorted by created_at descending, supports ?type= filtering.
    """
    memories = await repo.list_team_knowledge(
        team_id=team_id,
        memory_type=type or None,
        limit=limit,
    )
    return APIListResponse(data=memories, total=len(memories))


# ================================================================
# Agent experience summary endpoint
# ================================================================

router_agents_memory = APIRouter(prefix="/api/agents", tags=["memory"])


@router_agents_memory.get("/{agent_id}/experience", response_model=APIListResponse[Memory])
async def get_agent_experience(
    agent_id: str,
    limit: int = Query(50, ge=1, le=200, description="Return count limit"),
    repo: StorageRepository = Depends(get_repository),
) -> APIListResponse[Memory]:
    """Get Agent experience summary.

    Returns the Agent's scope=agent memory list,
    including task completion records and accumulated experience.
    """
    memories = await repo.list_agent_experience(agent_id=agent_id, limit=limit)
    return APIListResponse(data=memories, total=len(memories))
