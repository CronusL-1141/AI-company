"""AI Team OS — Agent管理路由."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from aiteam.api.deps import get_event_bus, get_manager, get_repository
from aiteam.api.event_bus import EventBus
from aiteam.api.schemas import AgentCreate, AgentStatusUpdate, APIListResponse, APIResponse
from aiteam.orchestrator.team_manager import TeamManager
from aiteam.storage.repository import StorageRepository
from aiteam.types import Agent, AgentStatus

router = APIRouter(tags=["agents"])


@router.get(
    "/api/teams/{team_id}/agents",
    response_model=APIListResponse[Agent],
)
async def list_agents(
    team_id: str,
    manager: TeamManager = Depends(get_manager),
) -> APIListResponse[Agent]:
    """列出团队中的所有Agent."""
    agents = await manager.list_agents(team_id)
    return APIListResponse(data=agents, total=len(agents))


@router.post(
    "/api/teams/{team_id}/agents",
    response_model=APIResponse[Agent],
    status_code=201,
)
async def add_agent(
    team_id: str,
    body: AgentCreate,
    manager: TeamManager = Depends(get_manager),
) -> APIResponse[Agent]:
    """向团队添加Agent."""
    agent = await manager.add_agent(
        team_name=team_id,
        name=body.name,
        role=body.role,
        system_prompt=body.system_prompt,
        model=body.model,
    )
    return APIResponse(data=agent, message="Agent添加成功")


@router.delete(
    "/api/agents/{agent_id}",
    response_model=APIResponse[bool],
)
async def remove_agent(
    agent_id: str,
    manager: TeamManager = Depends(get_manager),
    event_bus: EventBus = Depends(get_event_bus),
) -> APIResponse[bool]:
    """移除Agent（需要通过agent_id查找所属团队）."""
    # 通过repository直接删除
    repo = manager._repo
    # 先获取Agent信息用于事件发射
    agent = await repo.get_agent(agent_id)
    if agent is None:
        msg = f"Agent '{agent_id}' 不存在"
        raise ValueError(msg)
    result = await repo.delete_agent(agent_id)
    if result:
        await event_bus.emit(
            "agent.removed",
            f"agent:{agent_id}",
            {"agent_id": agent_id, "team_id": agent.team_id, "name": agent.name},
        )
    return APIResponse(data=True, message="Agent移除成功")


@router.put(
    "/api/agents/{agent_id}/status",
    response_model=APIResponse[Agent],
)
async def update_agent_status(
    agent_id: str,
    body: AgentStatusUpdate,
    repo: StorageRepository = Depends(get_repository),
    event_bus: EventBus = Depends(get_event_bus),
) -> APIResponse[Agent]:
    """更新Agent状态."""
    agent = await repo.update_agent(agent_id, status=AgentStatus(body.status))
    await event_bus.emit(
        "agent.status_changed",
        f"agent:{agent_id}",
        {"agent_id": agent_id, "team_id": agent.team_id, "status": body.status},
    )
    return APIResponse(data=agent, message="Agent状态更新成功")
