"""AI Team OS — 任务管理路由."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from aiteam.api.deps import get_manager, get_repository
from aiteam.api.schemas import APIListResponse, APIResponse, TaskDecompose, TaskRun
from aiteam.orchestrator.team_manager import TeamManager
from aiteam.storage.repository import StorageRepository
from aiteam.types import Task, TaskStatus

logger = logging.getLogger(__name__)

router = APIRouter(tags=["tasks"])

# ============================================================
# 内置拆解模板
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

    # 依赖检查：验证depends_on中的任务是否存在，并检测环
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

    # 创建任务记录（不执行LangGraph，交给CC Agent自行处理）
    title = body.title or body.description[:50]
    task = await repo.create_task(
        team_id=team.id,
        title=title,
        description=body.description,
        depends_on=body.depends_on,
        priority=body.priority,
        horizon=body.horizon,
        tags=body.tags,
    )

    # 如果有未完成的依赖，将状态设为BLOCKED
    if initial_status == TaskStatus.BLOCKED:
        task = await repo.update_task(task.id, status=TaskStatus.BLOCKED.value)

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
    """将任务拆解为父任务+子任务。

    可通过 template 指定内置模板，或通过 subtasks 自定义子任务列表。
    """
    # 确保 team 存在
    team = await manager.get_team(team_id)

    # 创建父任务 (depth=0)
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

    # 确定子任务列表
    subtask_specs: list[dict[str, Any]] = []

    if body.subtasks:
        # 用户自定义子任务
        for i, st in enumerate(body.subtasks):
            subtask_specs.append({
                "title": st.title,
                "description": st.description,
                "order": i,
                "role_hint": "",
            })
    elif body.template and body.template in DECOMPOSE_TEMPLATES:
        # 使用内置模板
        for tmpl in DECOMPOSE_TEMPLATES[body.template]:
            subtask_specs.append({
                "title": f"{body.title} — {tmpl['title_suffix']}",
                "description": f"{body.description}\n\n子任务: {tmpl['title_suffix']}" if body.description else tmpl["title_suffix"],
                "order": tmpl["order"],
                "role_hint": tmpl["role_hint"],
            })

    # 创建子任务 (depth=1, parent_id=父任务ID)
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
) -> dict[str, Any]:
    """标记任务为完成，并级联解锁下游BLOCKED任务."""
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

    # 更新为完成状态
    task = await repo.update_task(
        task_id,
        status=TaskStatus.COMPLETED.value,
        completed_at=datetime.now(),
    )

    # 级联解锁下游任务
    unblocked = await repo.resolve_task_dependencies(task_id)
    unblocked_info = [
        {"id": t.id, "title": t.title, "new_status": t.status.value}
        for t in unblocked
    ]

    if unblocked:
        logger.info(
            "任务 %s 完成，解锁 %d 个下游任务: %s",
            task_id,
            len(unblocked),
            [t.id for t in unblocked],
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
    """查询任务状态."""
    task = await manager.get_task_status(task_id)
    return APIResponse(data=task)


@router.get(
    "/api/tasks/{task_id}/subtasks",
)
async def get_subtasks(
    task_id: str,
    repo: StorageRepository = Depends(get_repository),
) -> dict[str, Any]:
    """获取任务的所有子任务。"""
    parent = await repo.get_task(task_id)
    if parent is None:
        raise HTTPException(status_code=404, detail=f"任务 {task_id} 不存在")

    # 查询所有以此任务为parent的子任务
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
