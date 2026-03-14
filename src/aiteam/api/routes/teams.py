"""AI Team OS — 团队管理路由."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from aiteam.api.deps import get_hook_translator, get_manager, get_repository
from aiteam.api.hook_translator import HookTranslator
from aiteam.api.schemas import (
    APIListResponse,
    APIResponse,
    TeamCreate,
    TeamUpdate,
)
from aiteam.orchestrator.team_manager import TeamManager
from aiteam.storage.repository import StorageRepository
from aiteam.types import AgentStatus, TaskStatus, Team, TeamStatusSummary

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
    repo: StorageRepository = Depends(get_repository),
) -> APIResponse[Team]:
    """创建团队.

    如果指定了leader_agent_id，自动完成该Leader的旧active团队。
    """
    # 自动完成Leader的旧active团队
    if body.leader_agent_id:
        old_team = await repo.find_active_team_by_leader(body.leader_agent_id)
        if old_team:
            from datetime import datetime
            await repo.update_team(
                old_team.id,
                status="completed",
                completed_at=datetime.now(),
            )

    team = await manager.create_team(
        name=body.name, mode=body.mode, config=body.config
    )
    # 设置project_id和leader关联
    updates: dict = {}
    if body.project_id:
        updates["project_id"] = body.project_id
    if body.leader_agent_id:
        updates["leader_agent_id"] = body.leader_agent_id
    if updates:
        team = await repo.update_team(team.id, **updates)

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
    repo: StorageRepository = Depends(get_repository),
) -> APIResponse[Team]:
    """更新团队（设置编排模式/状态）."""
    if body.mode is not None:
        team = await manager.set_mode(team_id, body.mode)
    else:
        team = await manager.get_team(team_id)

    # A13: 团队标记completed时自动将busy成员设为idle
    if body.status == "completed":
        from datetime import datetime

        team = await repo.update_team(
            team.id, status="completed", completed_at=datetime.now()
        )
        agents = await repo.list_agents(team.id)
        for agent in agents:
            if agent.status == AgentStatus.BUSY:
                await repo.update_agent(
                    agent.id, status="idle", current_task=None
                )
    elif body.status is not None:
        team = await repo.update_team(team.id, status=body.status)

    return APIResponse(data=team, message="团队更新成功")


@router.delete("/{team_id}", response_model=APIResponse[bool])
async def delete_team(
    team_id: str,
    manager: TeamManager = Depends(get_manager),
) -> APIResponse[bool]:
    """删除团队."""
    result = await manager.delete_team(team_id)
    return APIResponse(data=result, message="团队删除成功")


@router.get("/{team_id}/status", response_model=APIResponse[TeamStatusSummary])
async def get_status(
    team_id: str,
    manager: TeamManager = Depends(get_manager),
) -> APIResponse[TeamStatusSummary]:
    """获取团队状态摘要."""
    status = await manager.get_status(team_id)
    return APIResponse(data=status)


@router.get("/{team_id}/briefing")
async def team_briefing(
    team_id: str,
    manager: TeamManager = Depends(get_manager),
    repo: StorageRepository = Depends(get_repository),
    hook_translator: HookTranslator = Depends(get_hook_translator),
) -> dict[str, Any]:
    """获取团队全景简报 — 一次调用了解团队全部状态。

    聚合团队信息、成员状态、最近事件、最近会议、未完成任务和操作建议。
    """
    # 1. 团队基本信息（支持name或id查找）
    team = await manager.get_team(team_id)

    # 2. Agent列表（含状态和current_task）
    agents = await repo.list_agents(team.id)

    # 3. 最近10个事件（全局事件，无team_id过滤）
    events = await repo.list_events(limit=10)

    # 4. 最近一次会议
    meetings = await repo.list_meetings(team.id)
    recent_meeting = meetings[0] if meetings else None

    # 5. 未完成任务（pending + running + blocked）
    all_tasks = await repo.list_tasks(team.id)
    pending_tasks = [
        t for t in all_tasks
        if t.status in (TaskStatus.PENDING, TaskStatus.RUNNING, TaskStatus.BLOCKED)
    ]
    # 排序：ready任务(pending/running)在前，blocked在后
    ready_tasks = [t for t in pending_tasks if t.status != TaskStatus.BLOCKED]
    blocked_tasks = [t for t in pending_tasks if t.status == TaskStatus.BLOCKED]
    pending_tasks = ready_tasks + blocked_tasks

    # 6. 生成 _hints 建议文本
    idle_agents = [a for a in agents if a.status == AgentStatus.IDLE]
    busy_agents = [a for a in agents if a.status == AgentStatus.BUSY]
    hints: list[str] = []
    if idle_agents:
        names = ", ".join(a.name for a in idle_agents)
        hints.append(f"{len(idle_agents)}个agent空闲，可分配任务: {names}")
    if busy_agents:
        descs = ", ".join(
            f"{a.name}({a.current_task or '无描述'})" for a in busy_agents
        )
        hints.append(f"{len(busy_agents)}个agent工作中: {descs}")
    if ready_tasks or blocked_tasks:
        hints.append(f"{len(ready_tasks)}个任务可执行，{len(blocked_tasks)}个被阻塞")
    if not agents:
        hints.append("团队暂无成员，请先添加agent")

    # 7. 情境感知规则提醒（根据当前状态选择性提醒）
    if idle_agents and ready_tasks:
        hints.append("[规则] 有空闲agent和待办任务，可分配任务并行推进")
    if not ready_tasks and not blocked_tasks:
        hints.append("[规则] 任务不足，应组织会议讨论方向（loop_review），不能没事找事干")
    if len(idle_agents) > 3:
        hints.append("[规则] 空闲agent过多，考虑Kill不再需要的临时成员释放资源")
    if busy_agents and not idle_agents:
        hints.append("[规则] 全员忙碌，可动态添加新成员（必须用team_name）扩展产能")

    # 8. 热点文件检测（被多个agent编辑的文件）
    file_hotspots = hook_translator.get_file_hotspots(window_minutes=10)
    if file_hotspots:
        hotspot_desc = ", ".join(
            f"{h['file_path']}({'+'.join(h['agents'])})"
            for h in file_hotspots[:3]
        )
        hints.append(f"文件编辑热点: {hotspot_desc}")

    return {
        "success": True,
        "data": {
            "team": {
                "id": team.id,
                "name": team.name,
                "mode": team.mode.value if hasattr(team.mode, "value") else str(team.mode),
            },
            "agents": [
                {
                    "name": a.name,
                    "role": a.role,
                    "status": a.status.value if hasattr(a.status, "value") else str(a.status),
                    "current_task": a.current_task,
                    "source": a.source,
                }
                for a in agents
            ],
            "recent_events": [
                {
                    "type": e.type.value if hasattr(e.type, "value") else str(e.type),
                    "source": e.source,
                    "timestamp": e.timestamp.isoformat() if e.timestamp else None,
                    "data": e.data,
                }
                for e in events
            ],
            "recent_meeting": {
                "id": recent_meeting.id,
                "topic": recent_meeting.topic,
                "status": recent_meeting.status.value
                if hasattr(recent_meeting.status, "value")
                else str(recent_meeting.status),
                "created_at": recent_meeting.created_at.isoformat()
                if recent_meeting.created_at
                else None,
            }
            if recent_meeting
            else None,
            "pending_tasks": [
                {
                    "id": t.id,
                    "title": t.title,
                    "status": t.status.value if hasattr(t.status, "value") else str(t.status),
                    "assigned_to": t.assigned_to,
                    "depends_on": t.depends_on,
                    "depth": t.depth,
                    "parent_id": t.parent_id,
                }
                for t in pending_tasks
            ],
            "file_hotspots": file_hotspots,
            "_hints": "; ".join(hints),
        },
    }
