"""AI Team OS — Task wall routes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import PlainTextResponse

from aiteam.api.deps import get_repository, get_scoped_repository, get_task_wall_engine
from aiteam.api.schemas import APIListResponse
from aiteam.clock import utc_now
from aiteam.loop.auto_assign import TaskMatcher
from aiteam.loop.task_wall_engine import (
    TaskWallEngine,
    TaskWallRelations,
    backlog_sort_key,
    calculate_task_score,
    digest_payload,
    load_task_wall_digest,
    load_waiting_user_items,
    management_wall_fields,
    render_digest_text,
)
from aiteam.storage.repository import StorageRepository
from aiteam.types import DigestItem, Task, TaskStatus

router = APIRouter(tags=["task-wall"])

# Order of the unpaged open statuses after the pending page.
_OPEN_STATUS_ORDER = {
    TaskStatus.RUNNING.value: 0,
    TaskStatus.BLOCKED.value: 1,
    TaskStatus.FAILED.value: 2,
}


# The long text a board card never shows (fields=card): a third of a row, and all
# of the payload for tasks that carry a frozen memo archive in config.
_CARD_EXCLUDE = {"description", "result", "config"}


def _activity_fields(item: DigestItem) -> dict[str, Any]:
    """The digest's per-task activity, attached to a wall row for the Dashboard cards."""
    return {
        "last_activity_at": item.activity_at.isoformat(),
        "last_activity_kind": item.activity_kind,
        "last_activity_memo_type": item.activity_memo_type,
        "last_activity_by": item.activity_by,
        "idle_days": round(item.idle_days, 2),
        "wall_days": round(item.wall_days, 2),
        "stale": item.stale,
        "blocked_days": round(item.blocked_days, 2) if item.blocked_days is not None else None,
    }


def _completed_row(task: Task, team_name: str, relations: TaskWallRelations) -> dict[str, Any]:
    """A completed task as one short row; the detail dialog fetches the full task on open."""
    return {
        **relations.identity(task),
        "id": task.id,
        "title": task.title,
        "status": str(task.status),
        "priority": str(task.priority),
        "horizon": str(task.horizon),
        "assigned_to": task.assigned_to,
        "team_name": team_name,
        "created_at": task.created_at.isoformat(),
        "completed_at": task.completed_at.isoformat() if task.completed_at else None,
    }


@router.get("/api/teams/{team_id}/task-wall")
async def get_task_wall(
    team_id: str,
    horizon: str = "",
    priority: str = "",
    engine: TaskWallEngine = Depends(get_task_wall_engine),
) -> dict[str, Any]:
    """Get single-team task wall view.

    Returns {wall, stats} structure directly, aligned with frontend TaskWallResponse type.
    """
    result = await engine.get_task_wall(team_id, horizon=horizon, priority=priority)
    # engine.get_task_wall 返回 {"wall": {...}, "stats": {...}}
    return result


@router.get("/api/projects/{project_id}/task-wall")
async def get_project_task_wall(
    project_id: str,
    horizon: str = "",
    priority: str = "",
    limit: int = 50,
    offset: int = 0,
    include_completed: bool = False,
    status: str = "",
    completed_limit: int | None = None,
    completed_offset: int = 0,
    fields: str = "full",
    repo: StorageRepository = Depends(get_scoped_repository),
) -> dict[str, Any]:
    """Get project-level task wall view — query all tasks by project_id (including team_id=None project-level tasks).

    Returns {wall, completed, stats, digest, not_shown, has_more} structure directly,
    aligned with frontend TaskWallResponse type.

    limit/offset page the pending tasks only. Running, blocked and failed tasks always
    come back in full: they score 0, so a page cut after sorting by score used to drop
    every one of them (the briefing, task_list_project and the Dashboard all saw only
    pending rows). not_shown counts the pending rows left off this page. Pending rows
    run by score, ties to the task put up first; the rest follow, running, blocked,
    failed, each most recently active first.

    stats and digest describe the whole wall whatever the filters: every open
    task, including subtasks, once per task id.
    Open rows carry the digest's activity fields (last_activity_*, idle_days,
    wall_days, stale, blocked_days).

    Args:
        limit: Max number of pending tasks to return (default 50)
        offset: Pagination offset for pending tasks (default 0)
        include_completed: Include completed tasks in response (default False)
        status: Filter by status: pending/running/blocked/completed (default all active)
        completed_limit: With include_completed, page the completed tasks as short rows,
            newest first (default: every completed task as a full row)
        completed_offset: Offset into the completed short rows (default 0)
        fields: "full" (default) or "card": open rows without description, result and
            config, for a board that fetches a task in full only when it is opened
    """
    if fields not in ("full", "card"):
        raise HTTPException(status_code=400, detail="fields must be full or card")
    # Check if project exists
    project = await repo.get_project(project_id)
    if project is None:
        raise HTTPException(status_code=404, detail=f"Project '{project_id}' not found")

    # Resolve status filter
    status_filter: TaskStatus | None = None
    if status:
        try:
            status_filter = TaskStatus(status)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid status value: '{status}'")

    # Query all tasks directly by project_id, not by iterating teams
    all_project_tasks = await repo.list_tasks_by_project(project_id)

    # The digest covers the whole wall: a status filter narrows the rows, not the summary.
    now = utc_now()
    digest, activity = await load_task_wall_digest(
        repo, project_id,
        tasks=all_project_tasks,
        now=now,
    )

    # Build team_name mapping (for tasks with team_id)
    teams = await repo.list_teams_by_project(project_id)
    team_name_map: dict[str, str] = {t.id: t.name for t in teams}

    relations = TaskWallRelations.from_tasks(all_project_tasks)

    wall: dict[str, list[dict]] = {"short": [], "mid": [], "long": []}
    completed_tasks: list[dict] = []
    completed_source: list[Task] = []
    scores: list[float] = []
    # Every matching open task collected before pagination
    active_wall_items: list[dict] = []

    for task in all_project_tasks:
        if status_filter is not None and task.status != status_filter:
            continue
        h = str(task.horizon)
        if horizon and h != horizon:
            continue
        if priority and str(task.priority) not in priority.split(","):
            continue

        s = task.status if isinstance(task.status, str) else task.status.value

        if s == "completed":
            if not include_completed:
                continue
            if completed_limit is not None:
                completed_source.append(task)
                continue
            item = task.model_dump(mode="json")
            item["team_name"] = team_name_map.get(task.team_id, "") if task.team_id else ""
            item.update(relations.fields(task))
            item.update(management_wall_fields(task, relations.children.get(task.id, []), {}))
            completed_tasks.append(item)
            continue

        item = task.model_dump(mode="json", exclude=_CARD_EXCLUDE if fields == "card" else None)
        item["team_name"] = team_name_map.get(task.team_id, "") if task.team_id else ""
        score = calculate_task_score(task, now)
        item["score"] = round(score, 1)
        item["_horizon"] = h
        item["_rank"] = backlog_sort_key(task, now)
        if task.id in activity.items:
            item.update(_activity_fields(activity.items[task.id]))
        if s == TaskStatus.PENDING.value:
            scores.append(score)

        item.update(relations.fields(task))
        active_wall_items.append(item)

    # Page the pending tasks only: every other open status scores 0 and would sort
    # below the whole backlog, so a page cut would drop all of them.
    pending_items = [item for item in active_wall_items if item["status"] == TaskStatus.PENDING.value]
    other_items = [item for item in active_wall_items if item["status"] != TaskStatus.PENDING.value]
    pending_items.sort(key=lambda x: x["_rank"])
    other_items.sort(key=lambda x: x.get("last_activity_at") or "", reverse=True)
    other_items.sort(key=lambda x: _OPEN_STATUS_ORDER.get(x["status"], len(_OPEN_STATUS_ORDER)))
    page_start = max(offset, 0)
    paginated_pending = pending_items[page_start : page_start + max(limit, 0)]
    paginated_items = paginated_pending + other_items
    metadata = await repo.task_wall_management_metadata([item["id"] for item in paginated_items])
    tasks_by_id = {task.id: task for task in all_project_tasks}

    for item in paginated_items:
        item.update(management_wall_fields(
            tasks_by_id[item["id"]], relations.children.get(item["id"], []), metadata.get(item["id"], {}),
        ))
        h = item.pop("_horizon")
        item.pop("_rank")
        if h in wall:
            wall[h].append(item)

    # Completed tasks sorted by completion time descending
    completed_tasks.sort(
        key=lambda x: x.get("completed_at") or "",
        reverse=True,
    )
    completed_page: dict[str, Any] = {}
    if completed_limit is not None:
        completed_source.sort(key=lambda t: t.completed_at or t.created_at, reverse=True)
        start = max(completed_offset, 0)
        page = completed_source[start : start + max(completed_limit, 0)]
        completed_tasks = [
            _completed_row(t, team_name_map.get(t.team_id, "") if t.team_id else "", relations) for t in page
        ]
        completed_page = {
            "completed_total": len(completed_source),
            "completed_has_more": start + len(page) < len(completed_source),
        }

    stats = {
        "total": digest.open_total,
        "by_status": digest.by_status,
        "by_priority": digest.by_priority,
        "avg_score": round(sum(scores) / len(scores), 1) if scores else 0,
        "completed_count": digest.completed_total,
        "active_count": len(active_wall_items),
        "limit": limit,
        "offset": offset,
    }

    return {
        "wall": wall,
        "completed": completed_tasks,
        **completed_page,
        "stats": stats,
        "digest": digest_payload(digest),
        "not_shown": {"pending": len(pending_items) - len(paginated_pending)},
        "has_more": page_start + len(paginated_pending) < len(pending_items),
    }


@router.get("/api/projects/{project_id}/task-wall/digest", response_model=None)
async def get_project_task_wall_digest(
    project_id: str,
    format: str = "json",  # noqa: A002 - the query parameter's public name
    repo: StorageRepository = Depends(get_scoped_repository),
) -> dict[str, Any] | PlainTextResponse:
    """The task-wall digest alone: whole-wall counts, recent 5, top 5, stuck tasks.

    format=json returns the digest with its rendered text under "text"; format=text
    returns that text only (text/plain), the block the session briefing injects.
    Same numbers as the digest embedded in GET /task-wall.
    """
    if format not in ("json", "text"):
        raise HTTPException(status_code=400, detail="format must be json or text")
    project = await repo.get_project(project_id)
    if project is None:
        raise HTTPException(status_code=404, detail=f"Project '{project_id}' not found")
    digest, _ = await load_task_wall_digest(repo, project_id)
    if format == "text":
        return PlainTextResponse(render_digest_text(digest))
    return digest_payload(digest)


@router.get("/api/task-wall/waiting-user", response_model=APIListResponse[DigestItem])
async def list_waiting_user_tasks(
    project_id: str | None = None,
    repo: StorageRepository = Depends(get_repository),
) -> APIListResponse[DigestItem]:
    """Unfinished tasks waiting on the user, subtasks included, across every project.

    The rows the sidebar badge counts (``tasks`` of GET /api/notices/summary, on the
    same unscoped repository), each a digest row with its project_id, waiting_on and,
    for a subtask, its parent's id and title. ``project_id`` narrows to one project,
    a plain filter: the rows are that project's digest ``waiting_user``. Newest first.
    """
    items = await load_waiting_user_items(repo, utc_now(), project_id)
    return APIListResponse(data=items, total=len(items))


@router.get("/api/teams/{team_id}/task-matches")
async def get_task_matches(
    team_id: str,
    repo: StorageRepository = Depends(get_scoped_repository),
) -> dict[str, Any]:
    """Get task-Agent smart matching suggestions.

    Returns optimal match list of pending unassigned tasks with idle agents.
    Matching algorithm: keyword intersection scoring between Agent role and task tags.
    """
    matcher = TaskMatcher(repo)
    matches = await matcher.find_matches(team_id)
    return {
        "success": True,
        "data": matches,
        "total": len(matches),
    }
