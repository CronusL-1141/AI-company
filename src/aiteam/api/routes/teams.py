"""AI Team OS — Team management routes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from aiteam.api.deps import get_hook_translator, get_manager, get_repository, get_scoped_repository
from aiteam.api.hook_translator import HookTranslator
from aiteam.api.schemas import (
    APIListResponse,
    APIResponse,
    TeamUpdate,
)
from aiteam.loop.failure_alchemy import FailureAlchemist
from aiteam.orchestrator.team_manager import TeamManager
from aiteam.storage.repository import StorageRepository
from aiteam.types import AgentStatus, TaskStatus, Team, TeamStatusSummary

router = APIRouter(prefix="/api/teams", tags=["teams"])


@router.get("", response_model=APIListResponse[Team])
async def list_teams(
    repo: StorageRepository = Depends(get_scoped_repository),
) -> APIListResponse[Team]:
    """List all teams."""
    teams = await repo.list_teams()
    return APIListResponse(data=teams, total=len(teams))


# NOTE: POST /api/teams retired 2026-07-27 together with the `team_create` MCP tool.
# A team minted here was born broken: its config carried no `kind` key, so it fell
# outside the reaper's workflow/session exemptions, and CC never creates a matching
# ~/.claude/teams/<name>/ directory — the liveness probe closed it on the next cycle.
# Teams are created by the hook chain (SubagentStart auto-enrolment / workflow ingest),
# which stamps the right `kind`. Tests build teams straight through the repository.


@router.get("/{team_id}", response_model=APIResponse[Team])
async def get_team(
    team_id: str,
    manager: TeamManager = Depends(get_manager),
) -> APIResponse[Team]:
    """Get team details."""
    team = await manager.get_team(team_id)
    return APIResponse(data=team)


@router.put("/{team_id}", response_model=APIResponse[Team])
async def update_team(
    team_id: str,
    body: TeamUpdate,
    manager: TeamManager = Depends(get_manager),
    repo: StorageRepository = Depends(get_repository),
) -> APIResponse[Team]:
    """Update team (set orchestration mode/status)."""
    if body.mode is not None:
        team = await manager.set_mode(team_id, body.mode)
    else:
        team = await manager.get_team(team_id)

    # A13: Auto-set busy members to idle when team is marked completed
    if body.status == "completed":
        from datetime import datetime

        team = await repo.update_team(team.id, status="completed", completed_at=datetime.now())
        agents = await repo.list_agents(team.id)
        for agent in agents:
            if agent.status == AgentStatus.BUSY:
                await repo.update_agent(agent.id, status="offline", current_task=None)
    elif body.status is not None:
        team = await repo.update_team(team.id, status=body.status)

    # 项目归属改派/解绑（空串=解绑，等自动归属按权威路径重绑）。校验目标项目存在，
    # 防手滑写入不存在的 id 造成幽灵归属。
    if body.project_id is not None:
        target = body.project_id.strip()
        if target and await repo.get_project(target) is None:
            raise HTTPException(status_code=404, detail=f"项目不存在: {target}")
        team = await repo.update_team(team.id, project_id=target or None)

    return APIResponse(data=team, message="团队更新成功")


@router.delete("/{team_id}", response_model=APIResponse[bool])
async def delete_team(
    team_id: str,
    manager: TeamManager = Depends(get_manager),
) -> APIResponse[bool]:
    """Delete a team."""
    result = await manager.delete_team(team_id)
    return APIResponse(data=result, message="团队删除成功")


@router.get("/{team_id}/status", response_model=APIResponse[TeamStatusSummary])
async def get_status(
    team_id: str,
    manager: TeamManager = Depends(get_manager),
) -> APIResponse[TeamStatusSummary]:
    """Get team status summary."""
    status = await manager.get_status(team_id)
    return APIResponse(data=status)


@router.get("/{team_id}/briefing")
async def team_briefing(
    team_id: str,
    manager: TeamManager = Depends(get_manager),
    repo: StorageRepository = Depends(get_repository),
    hook_translator: HookTranslator = Depends(get_hook_translator),
) -> dict[str, Any]:
    """Get team panoramic briefing — understand full team status in one call.

    Aggregates team info, member status, recent events, recent meetings, pending tasks, and action hints.
    """
    # 1. Team basic info (supports name or id lookup)
    team = await manager.get_team(team_id)

    # 2. Agent list (with status and current_task)
    agents = await repo.list_agents(team.id)

    # 3. Recent 10 events for THIS team (was a global feed — a "team briefing"
    #    that hands back every other team's events is noise, and on a busy OS the
    #    10-row window was routinely filled by strangers).
    events = await repo.list_events(limit=10, team_ids=[team.id])

    # 4. Most recent meeting
    meetings = await repo.list_meetings(team.id)
    recent_meeting = meetings[0] if meetings else None

    # 5. Incomplete tasks (pending + running + blocked)
    all_tasks = await repo.list_tasks(team.id)
    pending_tasks = [
        t
        for t in all_tasks
        if t.status in (TaskStatus.PENDING, TaskStatus.RUNNING, TaskStatus.BLOCKED)
    ]
    # Sort: ready tasks (pending/running) first, blocked last
    ready_tasks = [t for t in pending_tasks if t.status != TaskStatus.BLOCKED]
    blocked_tasks = [t for t in pending_tasks if t.status == TaskStatus.BLOCKED]
    pending_tasks = ready_tasks + blocked_tasks

    # 6. Generate _hints suggestion text
    idle_agents = [a for a in agents if a.status == AgentStatus.WAITING]
    busy_agents = [a for a in agents if a.status == AgentStatus.BUSY]
    hints: list[str] = []
    if idle_agents:
        names = ", ".join(a.name for a in idle_agents)
        hints.append(f"{len(idle_agents)}个agent空闲，可分配任务: {names}")
    if busy_agents:
        descs = ", ".join(f"{a.name}({a.current_task or '无描述'})" for a in busy_agents)
        hints.append(f"{len(busy_agents)}个agent工作中: {descs}")
    if ready_tasks or blocked_tasks:
        hints.append(f"{len(ready_tasks)}个任务可执行，{len(blocked_tasks)}个被阻塞")
    if not agents:
        hints.append("团队暂无成员，直接用 CC 的 Agent 工具派发，SubagentStart 会自动收编入队")

    # 7. Context-aware rule reminders (selective reminders based on current state)
    #    文案口径按 CC v2.1.219 现状校准：Agent 的 team_name 参数已 Deprecated/ignored，
    #    每个会话自带隐式团队，"必须用 team_name"之类的旧话术会把 Leader 引向不存在的用法。
    if idle_agents and ready_tasks:
        hints.append("[规则] 有空闲agent和待办任务，可用 SendMessage(to='<成员名>') 续派并行推进")
    if not ready_tasks and not blocked_tasks:
        hints.append("[规则] 任务不足，应组织会议讨论方向（meeting_create），不能没事找事干")
    if len(idle_agents) > 3:
        hints.append("[规则] 空闲agent过多，考虑收掉不再需要的临时成员释放资源")
    if busy_agents and not idle_agents:
        hints.append("[规则] 全员忙碌，可直接 Agent(...) 再派新成员扩展产能（自动入队，无需建队）")

    # 8. File hotspot detection (files edited by multiple agents)
    file_hotspots = hook_translator.get_file_hotspots(window_minutes=10)
    if file_hotspots:
        hotspot_desc = ", ".join(
            f"{h['file_path']}({'+'.join(h['agents'])})" for h in file_hotspots[:3]
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


@router.get("/{team_id}/agent-intents")
async def get_agent_intents(
    team_id: str,
    repo: StorageRepository = Depends(get_repository),
) -> dict[str, Any]:
    """Get the latest intent event for each busy agent in the team (TOP2 Phase 2b).

    Returns the most recent intent.agent_working event for each agent,
    used for real-time Dashboard display of "what the Agent is doing".
    """
    agents = await repo.list_agents(team_id)
    busy_agents = [a for a in agents if a.status == AgentStatus.BUSY]

    intents: list[dict] = []
    for agent in busy_agents:
        # Find the agent's most recent intent event (source format: "agent:{agent_id}")
        events = await repo.list_events(
            event_type="intent.agent_working",
            source=f"agent:{agent.id}",
            limit=1,
        )
        if events:
            evt = events[0]
            intents.append(
                {
                    "agent_id": agent.id,
                    "agent_name": agent.name,
                    "tool_name": evt.data.get("tool_name", ""),
                    "intent_summary": evt.data.get("intent_summary", ""),
                    "input_preview": evt.data.get("input_preview", ""),
                    "timestamp": evt.timestamp.isoformat() if evt.timestamp else None,
                }
            )
        else:
            # Busy but no intent record, return basic info only
            intents.append(
                {
                    "agent_id": agent.id,
                    "agent_name": agent.name,
                    "tool_name": "",
                    "intent_summary": "",
                    "input_preview": "",
                    "timestamp": None,
                }
            )

    return {"success": True, "data": intents}


@router.post("/{team_id}/failure-analysis")
async def failure_analysis(
    team_id: str,
    body: dict[str, Any],
    repo: StorageRepository = Depends(get_repository),
) -> dict[str, Any]:
    """Perform failure alchemy analysis on failed tasks.

    Extracts defense rules, training cases, and improvement proposals.
    """
    task_id = body.get("task_id", "")
    if not task_id:
        return {"success": False, "error": "task_id is required"}

    alchemist = FailureAlchemist(repo)
    result = await alchemist.process_failure(task_id, team_id)
    return {"success": True, "data": result}
