"""AI Team OS — Task management routes."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from aiteam.api.deps import get_event_bus, get_manager, get_repository, get_scoped_repository
from aiteam.api.event_bus import EventBus
from aiteam.api.exceptions import NotFoundError
from aiteam.api.schemas import (
    APIListResponse,
    APIResponse,
    IssueReport,
    TaskCreateBody,
    TaskDecompose,
    TaskRun,
    TaskUpdateBody,
)
from aiteam.loop.what_if import WhatIfAnalyzer
from aiteam.orchestrator.team_manager import TeamManager
from aiteam.storage.repository import StorageRepository
from aiteam.types import Task, TaskStatus

logger = logging.getLogger(__name__)

router = APIRouter(tags=["tasks"])

# ============================================================
# Built-in decomposition templates
# ============================================================

DECOMPOSE_TEMPLATES: dict[str, list[dict[str, Any]]] = {
    "web-app": [
        {"title_suffix": "架构设计", "role_hint": "tech-lead", "order": 0},
        {"title_suffix": "后端API开发", "role_hint": "backend", "order": 1},
        {"title_suffix": "前端页面开发", "role_hint": "frontend", "order": 2},
        {"title_suffix": "集成测试", "role_hint": "qa", "order": 3},
        {"title_suffix": "部署配置", "role_hint": "devops", "order": 4},
    ],
    "api-service": [
        {"title_suffix": "接口设计", "role_hint": "tech-lead", "order": 0},
        {"title_suffix": "数据模型开发", "role_hint": "backend", "order": 1},
        {"title_suffix": "端点实现", "role_hint": "backend", "order": 2},
        {"title_suffix": "中间件与认证", "role_hint": "backend", "order": 3},
        {"title_suffix": "API测试", "role_hint": "qa", "order": 4},
    ],
    "data-pipeline": [
        {"title_suffix": "管道架构设计", "role_hint": "tech-lead", "order": 0},
        {"title_suffix": "数据采集", "role_hint": "data-engineer", "order": 1},
        {"title_suffix": "数据清洗与转换", "role_hint": "data-engineer", "order": 2},
        {"title_suffix": "数据加载与存储", "role_hint": "data-engineer", "order": 3},
        {"title_suffix": "数据质量验证", "role_hint": "qa", "order": 4},
    ],
    "library": [
        {"title_suffix": "API设计", "role_hint": "tech-lead", "order": 0},
        {"title_suffix": "核心实现", "role_hint": "developer", "order": 1},
        {"title_suffix": "文档编写", "role_hint": "developer", "order": 2},
        {"title_suffix": "单元测试", "role_hint": "qa", "order": 3},
    ],
    "refactor": [
        {"title_suffix": "影响分析", "role_hint": "tech-lead", "order": 0},
        {"title_suffix": "代码迁移", "role_hint": "developer", "order": 1},
        {"title_suffix": "依赖更新", "role_hint": "developer", "order": 2},
        {"title_suffix": "回归测试", "role_hint": "qa", "order": 3},
    ],
    "bugfix": [
        {"title_suffix": "问题复现", "role_hint": "developer", "order": 0},
        {"title_suffix": "根因定位", "role_hint": "developer", "order": 1},
        {"title_suffix": "修复实现", "role_hint": "developer", "order": 2},
        {"title_suffix": "验证测试", "role_hint": "qa", "order": 3},
    ],
}


@router.get(
    "/api/teams/{team_id}/tasks",
    response_model=APIListResponse[Task],
)
async def list_tasks(
    team_id: str,
    repo: StorageRepository = Depends(get_scoped_repository),
) -> APIListResponse[Task]:
    """List all tasks for a team."""
    tasks = await repo.list_tasks(team_id)
    return APIListResponse(data=tasks, total=len(tasks))


def _keyword_overlap(a: str, b: str) -> int:
    """Calculate keyword overlap count between two texts."""
    words_a = set(a.lower().split())
    words_b = set(b.lower().split())
    return len(words_a & words_b)


@router.post(
    "/api/teams/{team_id}/tasks/run",
)
async def run_task(
    team_id: str,
    body: TaskRun,
    manager: TeamManager = Depends(get_manager),
    repo: StorageRepository = Depends(get_scoped_repository),
    event_bus: EventBus = Depends(get_event_bus),
) -> dict[str, Any]:
    """Run a task, returning results and related tasks (duplicate detection)."""
    # Get team info to query running tasks
    team = await manager.get_team(team_id)
    running_tasks = await repo.list_tasks(team.id, status=TaskStatus.RUNNING)

    # Detect keyword overlap with running task titles (overlap >= 2 considered similar)
    related_tasks: list[dict[str, Any]] = []
    new_title = body.title or body.description[:50]
    for t in running_tasks:
        overlap = _keyword_overlap(new_title, t.title)
        if overlap >= 2:
            related_tasks.append(
                {
                    "id": t.id,
                    "title": t.title,
                    "status": t.status.value if hasattr(t.status, "value") else str(t.status),
                    "overlap_words": overlap,
                }
            )

    # Sort by overlap count, return at most 5
    related_tasks.sort(key=lambda x: x["overlap_words"], reverse=True)
    related_tasks = related_tasks[:5]

    # Dependency check: verify tasks in depends_on exist and detect cycles
    initial_status = TaskStatus.PENDING
    blocked_by: list[str] = []

    if body.depends_on:
        for dep_id in body.depends_on:
            dep_task = await repo.get_task(dep_id)
            if dep_task is None:
                raise HTTPException(
                    status_code=400,
                    detail=f"依赖任务 {dep_id} 不存在",
                )
            if dep_task.status != TaskStatus.COMPLETED:
                blocked_by.append(dep_id)

        if blocked_by:
            initial_status = TaskStatus.BLOCKED

    # Create task record (no LangGraph execution, left to CC Agent to handle)
    title = body.title or body.description[:50]
    create_kwargs: dict[str, Any] = {
        "team_id": team.id,
        "title": title,
        "description": body.description,
        "depends_on": body.depends_on,
        "priority": body.priority,
        "horizon": body.horizon,
        "tags": body.tags,
    }
    if body.assigned_to:
        create_kwargs["assigned_to"] = body.assigned_to
    task = await repo.create_task(**create_kwargs)

    # If there are incomplete dependencies, set status to BLOCKED
    if initial_status == TaskStatus.BLOCKED:
        task = await repo.update_task(task.id, status=TaskStatus.BLOCKED.value)

    # If assigned_to is specified, update that agent's current_task
    if body.assigned_to and initial_status != TaskStatus.BLOCKED:
        agents = await repo.list_agents(team.id)
        for agent in agents:
            if agent.name == body.assigned_to or agent.id == body.assigned_to:
                await repo.update_agent(agent.id, current_task=title)
                break

    # Emit task created event (triggers frontend real-time refresh)
    await event_bus.emit(
        "task.created",
        f"team:{team.id}",
        {"task_id": task.id, "team_id": team.id, "title": title},
    )

    # If assigned, also emit task.assigned event
    if body.assigned_to and initial_status != TaskStatus.BLOCKED:
        await event_bus.emit(
            "task.assigned",
            f"team:{team.id}",
            {
                "task_id": task.id,
                "team_id": team.id,
                "title": title,
                "assigned_to": body.assigned_to,
            },
        )

    message = "任务已创建，等待Agent领取执行"
    if blocked_by:
        message = f"任务已创建，但被 {len(blocked_by)} 个未完成的依赖阻塞"

    resp: dict[str, Any] = {
        "success": True,
        "data": task.model_dump(mode="json"),
        "message": message,
        "_hint": "任务已记录到团队任务列表。CC Agent可通过 team_briefing 查看待办任务并自行领取。",
    }
    if blocked_by:
        resp["blocked_by"] = blocked_by
    if related_tasks:
        resp["related_tasks"] = related_tasks
        resp["_warning"] = f"检测到{len(related_tasks)}个相似的运行中任务，请确认是否重复"
    return resp


@router.post(
    "/api/teams/{team_id}/tasks/decompose",
)
async def decompose_task(
    team_id: str,
    body: TaskDecompose,
    repo: StorageRepository = Depends(get_repository),
    manager: TeamManager = Depends(get_manager),
) -> dict[str, Any]:
    """Decompose a task into parent task + subtasks.

    Use template to specify a built-in template, or subtasks for a custom subtask list.
    """
    # Ensure team exists
    team = await manager.get_team(team_id)

    # Create parent task (depth=0)
    parent = await repo.create_task(
        team_id=team.id,
        title=body.title,
        description=body.description,
        depth=0,
        template_id=body.template or None,
        priority=body.priority,
        horizon=body.horizon,
        tags=body.tags,
    )

    # Determine subtask list
    subtask_specs: list[dict[str, Any]] = []

    if body.subtasks:
        # User-defined custom subtasks
        for i, st in enumerate(body.subtasks):
            subtask_specs.append(
                {
                    "title": st.title,
                    "description": st.description,
                    "order": i,
                    "role_hint": "",
                }
            )
    elif body.template and body.template in DECOMPOSE_TEMPLATES:
        # Use built-in template
        for tmpl in DECOMPOSE_TEMPLATES[body.template]:
            subtask_specs.append(
                {
                    "title": f"{body.title} — {tmpl['title_suffix']}",
                    "description": f"{body.description}\n\n子任务: {tmpl['title_suffix']}"
                    if body.description
                    else tmpl["title_suffix"],
                    "order": tmpl["order"],
                    "role_hint": tmpl["role_hint"],
                }
            )

    # Create subtasks (depth=1, parent_id=parent task ID)
    children: list[Task] = []
    for spec in subtask_specs:
        child = await repo.create_task(
            team_id=team.id,
            title=spec["title"],
            description=spec["description"],
            parent_id=parent.id,
            depth=1,
            order=spec["order"],
            template_id=body.template or None,
        )
        children.append(child)

    return {
        "success": True,
        "data": {
            "parent": parent.model_dump(mode="json"),
            "subtasks": [c.model_dump(mode="json") for c in children],
            "total_subtasks": len(children),
        },
        "message": f"任务已拆解为 {len(children)} 个子任务",
        "_hint": "子任务已创建，可通过 team_briefing 查看或分配给具体 Agent。",
    }


@router.put(
    "/api/tasks/{task_id}/complete",
)
async def complete_task(
    task_id: str,
    repo: StorageRepository = Depends(get_repository),
    event_bus: EventBus = Depends(get_event_bus),
) -> dict[str, Any]:
    """Mark task as completed and cascade-unlock downstream BLOCKED tasks."""
    task = await repo.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"任务 {task_id} 不存在")

    if task.status == TaskStatus.COMPLETED:
        return {
            "success": True,
            "data": task.model_dump(mode="json"),
            "message": "任务已经是完成状态",
            "unblocked_tasks": [],
        }

    # Update to completed status
    task = await repo.update_task(
        task_id,
        status=TaskStatus.COMPLETED.value,
        completed_at=datetime.now(),
    )

    # Clear assigned agent's current_task
    if task.assigned_to:
        agents = await repo.list_agents(task.team_id)
        for agent in agents:
            if agent.name == task.assigned_to or agent.id == task.assigned_to:
                await repo.update_agent(agent.id, current_task=None)
                break

    # Cascade-unlock downstream tasks
    unblocked = await repo.resolve_task_dependencies(task_id)
    unblocked_info = [
        {"id": t.id, "title": t.title, "new_status": t.status.value} for t in unblocked
    ]

    if unblocked:
        logger.info(
            "任务 %s 完成，解锁 %d 个下游任务: %s",
            task_id,
            len(unblocked),
            [t.id for t in unblocked],
        )

    # Emit task completed event (triggers frontend real-time refresh)
    await event_bus.emit(
        "task.completed",
        f"team:{task.team_id}",
        {"task_id": task_id, "team_id": task.team_id, "title": task.title},
    )

    # Emit task.status_changed event
    await event_bus.emit(
        "task.status_changed",
        f"team:{task.team_id}",
        {
            "task_id": task_id,
            "team_id": task.team_id,
            "title": task.title,
            "old_status": "running",
            "new_status": "completed",
        },
    )

    # Emit task.status_changed event for each unblocked downstream task
    for t in unblocked:
        await event_bus.emit(
            "task.status_changed",
            f"team:{task.team_id}",
            {
                "task_id": t.id,
                "team_id": task.team_id,
                "title": t.title,
                "old_status": "blocked",
                "new_status": t.status.value if hasattr(t.status, "value") else str(t.status),
            },
        )

    return {
        "success": True,
        "data": task.model_dump(mode="json"),
        "message": f"任务已完成，解锁了 {len(unblocked)} 个下游任务" if unblocked else "任务已完成",
        "unblocked_tasks": unblocked_info,
    }


@router.get(
    "/api/tasks/{task_id}",
    response_model=APIResponse[Task],
)
async def get_task_status(
    task_id: str,
    manager: TeamManager = Depends(get_manager),
) -> APIResponse[Task]:
    """Query task status."""
    task = await manager.get_task_status(task_id)
    return APIResponse(data=task)


@router.put(
    "/api/tasks/{task_id}",
)
async def update_task(
    task_id: str,
    body: TaskUpdateBody,
    repo: StorageRepository = Depends(get_repository),
    event_bus: EventBus = Depends(get_event_bus),
) -> dict[str, Any]:
    """Partial update a task — only provided fields are updated.

    Status transitions automatically set timestamps:
      - running  → started_at = now
      - completed → completed_at = now
    """
    task = await repo.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"任务 {task_id} 不存在")

    old_status = task.status.value if hasattr(task.status, "value") else str(task.status)

    update_kwargs: dict[str, Any] = {}

    if body.status is not None:
        # Validate status value
        try:
            TaskStatus(body.status)
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail=f"无效的 status 值: '{body.status}'，允许: {[s.value for s in TaskStatus]}",
            )
        update_kwargs["status"] = body.status
        if body.status == TaskStatus.RUNNING.value:
            update_kwargs["started_at"] = datetime.now()
        elif body.status == TaskStatus.COMPLETED.value:
            update_kwargs["completed_at"] = datetime.now()

    if body.assigned_to is not None:
        update_kwargs["assigned_to"] = body.assigned_to
    if body.result is not None:
        update_kwargs["result"] = body.result
    if body.priority is not None:
        update_kwargs["priority"] = body.priority
    if body.tags is not None:
        update_kwargs["tags"] = body.tags
    if body.title is not None:
        update_kwargs["title"] = body.title
    if body.description is not None:
        update_kwargs["description"] = body.description

    if not update_kwargs:
        return {
            "success": True,
            "data": task.model_dump(mode="json"),
            "message": "无字段需要更新",
        }

    task = await repo.update_task(task_id, **update_kwargs)

    # Emit status_changed event when status transitions
    new_status = task.status.value if hasattr(task.status, "value") else str(task.status)
    if "status" in update_kwargs and old_status != new_status:
        await event_bus.emit(
            "task.status_changed",
            f"team:{task.team_id}" if task.team_id else "global",
            {
                "task_id": task_id,
                "team_id": task.team_id,
                "title": task.title,
                "old_status": old_status,
                "new_status": new_status,
            },
        )

    return {
        "success": True,
        "data": task.model_dump(mode="json"),
        "message": "任务已更新",
    }


@router.get(
    "/api/tasks/{task_id}/subtasks",
)
async def get_subtasks(
    task_id: str,
    repo: StorageRepository = Depends(get_repository),
) -> dict[str, Any]:
    """Get all subtasks of a task."""
    parent = await repo.get_task(task_id)
    if parent is None:
        raise HTTPException(status_code=404, detail=f"任务 {task_id} 不存在")

    # Query all subtasks with this task as parent
    all_tasks = await repo.list_tasks(parent.team_id)
    subtasks = [t for t in all_tasks if t.parent_id == task_id]
    subtasks.sort(key=lambda t: t.order)

    return {
        "success": True,
        "data": {
            "parent_id": task_id,
            "subtasks": [t.model_dump(mode="json") for t in subtasks],
            "total": len(subtasks),
        },
    }


# ============================================================
# Project-level task creation endpoint
# ============================================================


@router.get(
    "/api/projects/{project_id}/tasks/running-count",
)
async def count_running_project_tasks(
    project_id: str,
    repo: StorageRepository = Depends(get_repository),
) -> dict[str, Any]:
    """Lightweight endpoint for hook use — returns count of running tasks under a project."""
    project = await repo.get_project(project_id)
    if not project:
        raise NotFoundError(f"项目 '{project_id}' 不存在")

    tasks = await repo.list_tasks_by_project(project_id, status=TaskStatus.RUNNING)
    return {"count": len(tasks)}


@router.post(
    "/api/projects/{project_id}/tasks",
    status_code=201,
)
async def create_project_task(
    project_id: str,
    body: TaskCreateBody,
    repo: StorageRepository = Depends(get_repository),
) -> dict[str, Any]:
    """Create a project-level task (not bound to a team)."""
    project = await repo.get_project(project_id)
    if not project:
        raise NotFoundError(f"项目 '{project_id}' 不存在")

    task = await repo.create_task(
        team_id=None,
        title=body.title,
        description=body.description,
        priority=body.priority,
        horizon=body.horizon,
        tags=body.tags,
        project_id=project_id,
    )
    return {"success": True, "data": task.model_dump(mode="json"), "message": "任务已创建"}


# ============================================================
# Issue endpoints — reuse Task model, distinguished by config.task_type="issue"
# ============================================================

_SEVERITY_TO_PRIORITY = {
    "critical": "critical",
    "high": "high",
    "medium": "high",
    "low": "medium",
}


@router.post("/api/teams/{team_id}/issues")
async def report_issue(
    team_id: str,
    body: IssueReport,
    manager: TeamManager = Depends(get_manager),
    repo: StorageRepository = Depends(get_repository),
) -> dict[str, Any]:
    """Report an issue — create a Task with task_type=issue."""
    team = await manager.get_team(team_id)
    priority = _SEVERITY_TO_PRIORITY.get(body.severity, "high")

    config = {
        "task_type": "issue",
        "severity": body.severity,
        "category": body.category,
        "source": "api",
        "resolution": "",
    }

    task = await repo.create_task(
        team_id=team.id,
        title=f"[Issue] {body.title}",
        description=body.description,
        priority=priority,
        horizon="short",
        tags=["issue", body.category],
        config=config,
    )

    return {
        "success": True,
        "data": task.model_dump(mode="json"),
        "message": f"Issue 已上报，优先级: {priority}",
    }


@router.get("/api/teams/{team_id}/issues")
async def list_issues(
    team_id: str,
    manager: TeamManager = Depends(get_manager),
    repo: StorageRepository = Depends(get_repository),
) -> dict[str, Any]:
    """List all issues for a team (filtered by config.task_type=issue)."""
    team = await manager.get_team(team_id)
    all_tasks = await repo.list_tasks(team.id)
    issues = [t for t in all_tasks if t.config.get("task_type") == "issue"]

    return {
        "success": True,
        "data": [t.model_dump(mode="json") for t in issues],
        "total": len(issues),
    }


# Valid issue status transition mapping
_ISSUE_TRANSITIONS: dict[str, list[str]] = {
    "open": ["investigating", "in_progress", "resolved"],
    "investigating": ["in_progress", "resolved", "open"],
    "in_progress": ["resolved", "investigating"],
    "resolved": ["verified", "open"],  # Can be reopened
    "verified": [],  # Terminal state
}


@router.put("/api/issues/{issue_id}/status")
async def update_issue_status(
    issue_id: str,
    body: dict[str, Any],
    repo: StorageRepository = Depends(get_repository),
) -> dict[str, Any]:
    """Update issue status: open->investigating->in_progress->resolved->verified."""
    task = await repo.get_task(issue_id)
    if task is None or task.config.get("task_type") != "issue":
        raise HTTPException(status_code=404, detail="Issue不存在")

    new_status = body.get("status")
    if not new_status:
        raise HTTPException(status_code=400, detail="缺少 status 字段")

    current_status = task.config.get("issue_status", "open")
    allowed = _ISSUE_TRANSITIONS.get(current_status, [])
    if new_status not in allowed:
        raise HTTPException(
            status_code=400,
            detail=f"不允许从 '{current_status}' 转为 '{new_status}'，允许: {allowed}",
        )

    resolution = body.get("resolution", "")

    # Merge-update config (without overwriting other fields)
    config = dict(task.config)
    config["issue_status"] = new_status
    if resolution:
        config["resolution"] = resolution

    update_kwargs: dict[str, Any] = {"config": config}

    # Mark task completed on resolved / verified
    if new_status in ("resolved", "verified"):
        update_kwargs["status"] = TaskStatus.COMPLETED.value
        update_kwargs["completed_at"] = datetime.now()

    # Restore to pending when reopened
    if new_status == "open" and current_status in ("resolved",):
        update_kwargs["status"] = TaskStatus.PENDING.value
        update_kwargs["completed_at"] = None

    task = await repo.update_task(issue_id, **update_kwargs)

    return {
        "success": True,
        "data": task.model_dump(mode="json"),
        "message": f"Issue 状态已更新: {current_status} → {new_status}",
    }


@router.get("/api/tasks/{task_id}/execution-trace")
async def get_execution_trace(
    task_id: str,
    repo: StorageRepository = Depends(get_repository),
) -> dict[str, Any]:
    """Get complete execution timeline for a task.

    Returns a unified timeline of all memo records sorted by timestamp,
    giving a complete picture of who did what, when, and with what result.
    """
    task = await repo.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"任务 {task_id} 不存在")

    memos: list[dict[str, Any]] = task.config.get("memo", [])

    timeline: list[dict[str, Any]] = []
    for memo in memos:
        timeline.append(
            {
                "timestamp": memo.get("timestamp", ""),
                "type": "memo",
                "author": memo.get("author", ""),
                "content": memo.get("content", ""),
                "memo_type": memo.get("type", ""),
            }
        )

    # Add task lifecycle events from task metadata
    if task.created_at:
        timeline.append(
            {
                "timestamp": task.created_at.isoformat(),
                "type": "task_event",
                "author": task.assigned_to or "system",
                "content": f"任务创建: {task.title}",
                "memo_type": "created",
            }
        )
    if task.started_at:
        timeline.append(
            {
                "timestamp": task.started_at.isoformat(),
                "type": "task_event",
                "author": task.assigned_to or "system",
                "content": "任务开始执行",
                "memo_type": "started",
            }
        )
    if task.completed_at:
        timeline.append(
            {
                "timestamp": task.completed_at.isoformat(),
                "type": "task_event",
                "author": task.assigned_to or "system",
                "content": "任务完成",
                "memo_type": "completed",
            }
        )

    timeline.sort(key=lambda x: x["timestamp"])

    return {
        "success": True,
        "data": {
            "task": task.model_dump(mode="json"),
            "timeline": timeline,
            "total_events": len(timeline),
        },
    }


@router.get("/api/tasks/{task_id}/what-if")
async def what_if_analysis(
    task_id: str,
    team_id: str = "",
    repo: StorageRepository = Depends(get_repository),
    manager: TeamManager = Depends(get_manager),
) -> dict[str, Any]:
    """Perform What-If analysis on a task — generate multi-plan comparison and recommendation."""
    task = await repo.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"任务 {task_id} 不存在")

    # Resolve team_id: prefer parameter, fallback to task's own team_id
    resolved_team_id = team_id or (task.team_id or "")
    if not resolved_team_id:
        raise HTTPException(status_code=400, detail="缺少 team_id，请通过查询参数传入")

    analyzer = WhatIfAnalyzer(repo)
    result = await analyzer.analyze_task(task_id, resolved_team_id)

    if "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])

    return {"success": True, "data": result}
