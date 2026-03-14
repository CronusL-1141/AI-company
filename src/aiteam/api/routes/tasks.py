"""AI Team OS — 任务管理路由."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from aiteam.api.deps import get_manager, get_repository
from aiteam.api.schemas import APIListResponse, APIResponse, TaskRun
from aiteam.orchestrator.team_manager import TeamManager
from aiteam.storage.repository import StorageRepository
from aiteam.types import Task, TaskStatus

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


def _keyword_overlap(a: str, b: str) -> int:
    """计算两个文本的关键词重叠数."""
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
    repo: StorageRepository = Depends(get_repository),
) -> dict[str, Any]:
    """运行任务，返回结果和相关任务（重复检测）."""
    # 获取团队信息以查询正在运行的任务
    team = await manager.get_team(team_id)
    running_tasks = await repo.list_tasks(team.id, status=TaskStatus.RUNNING)

    # 检测与正在运行任务的标题关键词重叠（重叠词数>=2视为相似）
    related_tasks: list[dict[str, Any]] = []
    new_title = body.title or body.description[:50]
    for t in running_tasks:
        overlap = _keyword_overlap(new_title, t.title)
        if overlap >= 2:
            related_tasks.append({
                "id": t.id,
                "title": t.title,
                "status": t.status.value if hasattr(t.status, "value") else str(t.status),
                "overlap_words": overlap,
            })

    # 按重叠数排序，最多返回5条
    related_tasks.sort(key=lambda x: x["overlap_words"], reverse=True)
    related_tasks = related_tasks[:5]

    # 创建任务记录（不执行LangGraph，交给CC Agent自行处理）
    title = body.title or body.description[:50]
    task = await repo.create_task(
        team_id=team.id,
        title=title,
        description=body.description,
    )

    resp: dict[str, Any] = {
        "success": True,
        "data": task.model_dump(mode="json"),
        "message": "任务已创建，等待Agent领取执行",
        "_hint": "任务已记录到团队任务列表。CC Agent可通过 team_briefing 查看待办任务并自行领取。",
    }
    if related_tasks:
        resp["related_tasks"] = related_tasks
        resp["_warning"] = f"检测到{len(related_tasks)}个相似的运行中任务，请确认是否重复"
    return resp


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
