"""AI Team OS — 团队管理路由."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from aiteam.api.deps import get_manager
from aiteam.api.schemas import (
    APIListResponse,
    APIResponse,
    TeamCreate,
    TeamUpdate,
)
from aiteam.orchestrator.team_manager import TeamManager
from aiteam.types import Team, TeamStatus

router = APIRouter(prefix="/api/teams", tags=["teams"])


@router.get("", response_model=APIListResponse[Team])
async def list_teams(
    manager: TeamManager = Depends(get_manager),
) -> APIListResponse[Team]:
    """列出所有团队."""
    teams = await manager.list_teams()
    return APIListResponse(data=teams, total=len(teams))


@router.post("", response_model=APIResponse[Team], status_code=201)
async def create_team(
    body: TeamCreate,
    manager: TeamManager = Depends(get_manager),
) -> APIResponse[Team]:
    """创建团队."""
    team = await manager.create_team(
        name=body.name, mode=body.mode, config=body.config
    )
    return APIResponse(data=team, message="团队创建成功")


@router.get("/{team_id}", response_model=APIResponse[Team])
async def get_team(
    team_id: str,
    manager: TeamManager = Depends(get_manager),
) -> APIResponse[Team]:
    """获取团队详情."""
    team = await manager.get_team(team_id)
    return APIResponse(data=team)


@router.put("/{team_id}", response_model=APIResponse[Team])
async def update_team(
    team_id: str,
    body: TeamUpdate,
    manager: TeamManager = Depends(get_manager),
) -> APIResponse[Team]:
    """更新团队（设置编排模式）."""
    if body.mode is not None:
        team = await manager.set_mode(team_id, body.mode)
    else:
        team = await manager.get_team(team_id)
    return APIResponse(data=team, message="团队更新成功")


@router.delete("/{team_id}", response_model=APIResponse[bool])
async def delete_team(
    team_id: str,
    manager: TeamManager = Depends(get_manager),
) -> APIResponse[bool]:
    """删除团队."""
    result = await manager.delete_team(team_id)
    return APIResponse(data=result, message="团队删除成功")


@router.get("/{team_id}/status", response_model=APIResponse[TeamStatus])
async def get_status(
    team_id: str,
    manager: TeamManager = Depends(get_manager),
) -> APIResponse[TeamStatus]:
    """获取团队状态摘要."""
    status = await manager.get_status(team_id)
    return APIResponse(data=status)
