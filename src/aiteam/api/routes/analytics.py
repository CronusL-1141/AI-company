"""AI Team OS — Data analytics routes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query

from aiteam.api.deps import get_repository
from aiteam.storage.repository import StorageRepository

router = APIRouter(prefix="/api/analytics", tags=["analytics"])


@router.get("/token-costs")
async def get_token_costs(
    group_by: str = Query("agent", pattern="^(agent|team|task)$"),
    days: int = Query(7, ge=1, le=90),
    repo: StorageRepository = Depends(get_repository),
) -> dict[str, Any]:
    """Token consumption and cost analytics."""
    data = await repo.get_token_costs(group_by=group_by, days=days)
    return {"success": True, "data": data, "group_by": group_by, "days": days}


@router.get("/tool-usage")
async def get_tool_usage(
    team_id: str | None = Query(None),
    repo: StorageRepository = Depends(get_repository),
) -> dict[str, Any]:
    """Tool usage distribution statistics."""
    data = await repo.count_activities_by_tool(team_id=team_id)
    return {"success": True, "data": data}


@router.get("/agent-productivity")
async def get_agent_productivity(
    team_id: str | None = Query(None),
    repo: StorageRepository = Depends(get_repository),
) -> dict[str, Any]:
    """Agent productivity metrics."""
    data = await repo.get_agent_productivity(team_id=team_id)
    return {"success": True, "data": data}


@router.get("/timeline")
async def get_activity_timeline(
    team_id: str | None = Query(None),
    hours: int = Query(24, ge=1, le=168),
    repo: StorageRepository = Depends(get_repository),
) -> dict[str, Any]:
    """Activity timeline (aggregated by hour)."""
    data = await repo.get_activity_timeline(team_id=team_id, hours=hours)
    return {"success": True, "data": data}


@router.get("/efficiency")
async def get_efficiency_metrics(
    team_id: str | None = Query(None),
    repo: StorageRepository = Depends(get_repository),
) -> dict[str, Any]:
    """Efficiency analysis metrics."""
    # 1. Task completion rate + average completion time
    task_stats = await repo.get_task_completion_stats(team_id=team_id)

    # 2. Agent utilization
    agent_utilization = await repo.get_agent_utilization(team_id=team_id)

    # 3. Tool call efficiency: average tool calls per completed task
    productivity = await repo.get_agent_productivity(team_id=team_id)
    total_activities = sum(p["activity_count"] for p in productivity)
    completed = task_stats["completed_tasks"]
    avg_tools_per_task = round(total_activities / completed, 2) if completed > 0 else None

    # 4. Top efficient agents (by activities_per_hour desc)
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


@router.get("/budget")
async def get_budget_status(
    repo: StorageRepository = Depends(get_repository),
) -> dict[str, Any]:
    """Get current budget utilization and end-of-week forecast."""
    from aiteam.integrations.budget import check_budget

    result = await check_budget(repo)
    return {"success": True, "data": result}


@router.get("/team-overview")
async def get_team_overview(
    team_id: str = Query(...),
    repo: StorageRepository = Depends(get_repository),
) -> dict[str, Any]:
    """Team overall statistics overview."""
    # Tool distribution
    tool_dist = await repo.count_activities_by_tool(team_id=team_id)

    # Agent productivity
    productivity = await repo.get_agent_productivity(team_id=team_id)

    # Active agent count
    agents = await repo.list_agents(team_id)
    active_agents = [a for a in agents if a.status.value == "busy"]

    # Total activity count
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
