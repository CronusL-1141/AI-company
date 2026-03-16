"""AI Team OS — Agent管理路由."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends

from aiteam.api.deps import get_event_bus, get_manager, get_repository
from aiteam.api.event_bus import EventBus
from aiteam.api.schemas import AgentCreate, AgentStatusUpdate, APIListResponse, APIResponse
from aiteam.orchestrator.team_manager import TeamManager
from aiteam.storage.repository import StorageRepository
from aiteam.types import Agent, AgentStatus, TaskStatus

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
    status_code=201,
)
async def add_agent(
    team_id: str,
    body: AgentCreate,
    manager: TeamManager = Depends(get_manager),
    repo: StorageRepository = Depends(get_repository),
) -> dict[str, Any]:
    """向团队添加Agent，返回agent信息和团队快照.

    去重：同一团队内不允许同名agent。如果已存在同名agent，
    更新其信息并返回（覆盖hook自动注册的占位数据）。
    """
    team = await manager.get_team(team_id)
    existing_agents = await repo.list_agents(team.id)
    existing = next((a for a in existing_agents if a.name == body.name), None)

    # 如果role含 " — "，自动分割为role + current_task
    effective_role = body.role
    auto_task: str | None = None
    if body.role and " — " in body.role:
        parts = body.role.split(" — ", 1)
        effective_role = parts[0].strip()
        auto_task = parts[1].strip()

    if existing:
        # 同名agent已存在（可能由hook自动注册）→ 更新而非重复创建
        update_fields: dict[str, object] = {
            "status": "busy",
            "last_active_at": datetime.now(),
        }
        if effective_role:
            update_fields["role"] = effective_role
        if auto_task:
            update_fields["current_task"] = auto_task
        if body.system_prompt:
            update_fields["system_prompt"] = body.system_prompt
        if body.model:
            update_fields["model"] = body.model
        # 标记为api来源（MCP注册优先级高于hook自动注册）
        update_fields["source"] = "api"
        agent = await repo.update_agent(existing.id, **update_fields)
    else:
        agent = await manager.add_agent(
            team_name=team_id,
            name=body.name,
            role=effective_role,
            system_prompt=body.system_prompt,
            model=body.model,
        )
        # 注册即工作 — 默认设为busy
        update_kwargs: dict[str, object] = {"status": "busy", "last_active_at": datetime.now()}
        if auto_task:
            update_kwargs["current_task"] = auto_task
        await repo.update_agent(agent.id, **update_kwargs)

    # 获取团队快照：当前所有agent、待办任务和最近会议
    team = await manager.get_team(team_id)
    all_agents = await repo.list_agents(team.id)
    all_tasks = await repo.list_tasks(team.id)
    pending_tasks = [
        t for t in all_tasks
        if t.status in (TaskStatus.PENDING, TaskStatus.RUNNING)
    ]

    # 最近一次会议
    meetings = await repo.list_meetings(team.id)
    recent_meeting = meetings[0] if meetings else None

    # 构建teammates列表（排除刚注册的自己）
    teammates = [
        {
            "name": a.name,
            "role": a.role,
            "status": a.status.value if hasattr(a.status, "value") else str(a.status),
            "current_task": a.current_task,
        }
        for a in all_agents
        if a.id != agent.id
    ]

    return {
        "success": True,
        "data": agent.model_dump(mode="json"),
        "message": "Agent添加成功",
        "teammates": teammates,
        "team_snapshot": {
            "agents": [
                {
                    "name": a.name,
                    "status": a.status.value if hasattr(a.status, "value") else str(a.status),
                }
                for a in all_agents
            ],
            "pending_tasks": [
                {
                    "id": t.id,
                    "title": t.title,
                    "status": t.status.value if hasattr(t.status, "value") else str(t.status),
                    "assigned_to": t.assigned_to,
                }
                for t in pending_tasks
            ],
            "recent_meeting": {
                "id": recent_meeting.id,
                "topic": recent_meeting.topic,
                "status": recent_meeting.status.value
                if hasattr(recent_meeting.status, "value")
                else str(recent_meeting.status),
            }
            if recent_meeting
            else None,
        },
    }


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
        from aiteam.api.exceptions import NotFoundError
        msg = f"Agent '{agent_id}' 不存在"
        raise NotFoundError(msg)
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
    update_kwargs: dict[str, object] = {"status": AgentStatus(body.status)}
    if body.current_task is not None:
        update_kwargs["current_task"] = body.current_task
    agent = await repo.update_agent(agent_id, **update_kwargs)
    await event_bus.emit(
        "agent.status_changed",
        f"agent:{agent_id}",
        {"agent_id": agent_id, "team_id": agent.team_id, "status": body.status},
    )
    return APIResponse(data=agent, message="Agent状态更新成功")
