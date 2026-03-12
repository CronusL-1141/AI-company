"""AI Team OS — 任务管理路由."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from aiteam.api.deps import get_manager
from aiteam.api.schemas import APIListResponse, APIResponse, TaskRun
from aiteam.orchestrator.team_manager import TeamManager
from aiteam.types import Task, TaskResult

router = APIRouter(tags=["tasks"])


@router.get(
    "/api/teams/{team_id}/tasks",
    response_model=APIListResponse[Task],
)
async def list_tasks(
    team_id: str,
    manager: TeamManager = Depends(get_manager),
) -> APIListResponse[Task]:
    """列出团队的所有任务."""
    tasks = await manager.list_tasks(team_id)
    return APIListResponse(data=tasks, total=len(tasks))


@router.post(
    "/api/teams/{team_id}/tasks/run",
    response_model=APIResponse[TaskResult],
)
async def run_task(
    team_id: str,
    body: TaskRun,
    manager: TeamManager = Depends(get_manager),
) -> APIResponse[TaskResult]:
    """运行任务."""
    kwargs = {}
    if body.title:
        kwargs["title"] = body.title
    if body.model:
        kwargs["model"] = body.model
    result = await manager.run_task(
        team_name=team_id,
        task_description=body.description,
        **kwargs,
    )
    return APIResponse(data=result, message="任务执行完成")


@router.get(
    "/api/tasks/{task_id}",
    response_model=APIResponse[Task],
)
async def get_task_status(
    task_id: str,
    manager: TeamManager = Depends(get_manager),
) -> APIResponse[Task]:
    """查询任务状态."""
    task = await manager.get_task_status(task_id)
    return APIResponse(data=task)
