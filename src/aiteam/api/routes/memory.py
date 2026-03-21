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


# ================================================================
# 团队知识库端点
# ================================================================

router_teams_memory = APIRouter(prefix="/api/teams", tags=["memory"])


@router_teams_memory.get("/{team_id}/knowledge", response_model=APIListResponse[Memory])
async def get_team_knowledge(
    team_id: str,
    type: str = Query("", description="类型过滤：failure_alchemy / lesson_learned / loop_review"),
    limit: int = Query(50, ge=1, le=200, description="返回数量限制"),
    repo: StorageRepository = Depends(get_repository),
) -> APIListResponse[Memory]:
    """获取团队知识库.

    返回该团队 scope=team 的记忆列表，包含：
    - failure_alchemy 产生的失败教训
    - lesson_learned 手动记录的经验
    - loop_review 回顾总结
    按 created_at 降序排列，支持 ?type= 过滤。
    """
    memories = await repo.list_team_knowledge(
        team_id=team_id,
        memory_type=type or None,
        limit=limit,
    )
    return APIListResponse(data=memories, total=len(memories))


# ================================================================
# Agent 经验摘要端点
# ================================================================

router_agents_memory = APIRouter(prefix="/api/agents", tags=["memory"])


@router_agents_memory.get("/{agent_id}/experience", response_model=APIListResponse[Memory])
async def get_agent_experience(
    agent_id: str,
    limit: int = Query(50, ge=1, le=200, description="返回数量限制"),
    repo: StorageRepository = Depends(get_repository),
) -> APIListResponse[Memory]:
    """获取 Agent 经验摘要.

    返回该 Agent scope=agent 的记忆列表，
    包含其参与的任务完成记录和经验沉淀。
    """
    memories = await repo.list_agent_experience(agent_id=agent_id, limit=limit)
    return APIListResponse(data=memories, total=len(memories))
