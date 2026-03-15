"""AI Team OS — 数据分析统计路由."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query

from aiteam.api.deps import get_repository
from aiteam.storage.repository import StorageRepository

router = APIRouter(prefix="/api/analytics", tags=["analytics"])


@router.get("/tool-usage")
async def get_tool_usage(
    team_id: str | None = Query(None),
    repo: StorageRepository = Depends(get_repository),
) -> dict[str, Any]:
    """工具使用分布统计."""
    data = await repo.count_activities_by_tool(team_id=team_id)
    return {"success": True, "data": data}


@router.get("/agent-productivity")
async def get_agent_productivity(
    team_id: str | None = Query(None),
    repo: StorageRepository = Depends(get_repository),
) -> dict[str, Any]:
    """Agent产能指标."""
    data = await repo.get_agent_productivity(team_id=team_id)
    return {"success": True, "data": data}


@router.get("/timeline")
async def get_activity_timeline(
    team_id: str | None = Query(None),
    hours: int = Query(24, ge=1, le=168),
    repo: StorageRepository = Depends(get_repository),
) -> dict[str, Any]:
    """活动时间线（按小时聚合）."""
    data = await repo.get_activity_timeline(team_id=team_id, hours=hours)
    return {"success": True, "data": data}


@router.get("/efficiency")
async def get_efficiency_metrics(
    team_id: str | None = Query(None),
    repo: StorageRepository = Depends(get_repository),
) -> dict[str, Any]:
    """效率分析指标."""
    # 1. 任务完成率 + 平均完成时间
    task_stats = await repo.get_task_completion_stats(team_id=team_id)

    # 2. Agent利用率
    agent_utilization = await repo.get_agent_utilization(team_id=team_id)

    # 3. 工具调用效率：完成任务的平均工具调用次数
    productivity = await repo.get_agent_productivity(team_id=team_id)
    total_activities = sum(p["activity_count"] for p in productivity)
    completed = task_stats["completed_tasks"]
    avg_tools_per_task = (
        round(total_activities / completed, 2) if completed > 0 else None
    )

    # 4. 最高效Agent排行（按activities_per_hour降序）
    top_agents = sorted(
        agent_utilization,
        key=lambda a: a["activities_per_hour"],
        reverse=True,
    )[:5]

    return {
        "success": True,
        "data": {
            "task_completion": task_stats,
            "avg_tools_per_task": avg_tools_per_task,
            "agent_utilization": agent_utilization,
            "top_agents": top_agents,
        },
    }


@router.get("/team-overview")
async def get_team_overview(
    team_id: str = Query(...),
    repo: StorageRepository = Depends(get_repository),
) -> dict[str, Any]:
    """团队整体统计概览."""
    # 工具分布
    tool_dist = await repo.count_activities_by_tool(team_id=team_id)

    # Agent产能
    productivity = await repo.get_agent_productivity(team_id=team_id)

    # 活跃Agent数
    agents = await repo.list_agents(team_id)
    active_agents = [a for a in agents if a.status.value == "busy"]

    # 总活动数
    total_activities = sum(p["activity_count"] for p in productivity)

    return {
        "success": True,
        "data": {
            "total_activities": total_activities,
            "total_agents": len(agents),
            "active_agents": len(active_agents),
            "tool_distribution": tool_dist,
            "agent_productivity": productivity,
        },
    }
