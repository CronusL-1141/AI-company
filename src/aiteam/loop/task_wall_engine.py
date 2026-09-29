"""AI Team OS - Task wall backend (scoring, digest + team-scoped wall projection).

Split out of the retired loop state machine (2026-07-27 scheduler slim-down).
The scoring function and the wall projection are the live task-wall backend used
by `GET /api/teams/{team_id}/task-wall`, `GET /api/projects/{id}/task-wall` and
the task-wall MCP tool (task_list_project) — they never belonged to the loop phase machine.

The digest (build_task_wall_digest + render_digest_text) is the one summary of a
project's wall that the session briefing, the post-compaction briefing,
task_list_project, the /loop patrol and the Dashboard all read. Its rules are in
docs/task-wall-digest-design.md; the docstrings below restate the ones a test pins.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from aiteam.clock import utc_now
from aiteam.text_safety import clean_text
from aiteam.types import (
    DigestItem,
    Task,
    TaskActivityKind,
    TaskActivityRecord,
    TaskHorizon,
    TaskPriority,
    TaskStatus,
    TaskWallDigest,
)

# Priority weights
PRIORITY_WEIGHTS = {
    TaskPriority.CRITICAL: 100,
    TaskPriority.HIGH: 40,
    TaskPriority.MEDIUM: 10,
    TaskPriority.LOW: 2,
}

HORIZON_WEIGHTS = {
    TaskHorizon.SHORT: 3.0,
    TaskHorizon.MID: 1.5,
    TaskHorizon.LONG: 0.8,
}


def calculate_task_score(task: Task, now: datetime | None = None) -> float:
    """Calculate composite sorting score for a task; higher means higher priority."""
    if now is None:
        now = utc_now()

    if task.status not in (TaskStatus.PENDING,):
        return 0.0

    priority_w = PRIORITY_WEIGHTS.get(
        TaskPriority(task.priority) if isinstance(task.priority, str) else task.priority,
        10,
    )
    horizon_w = HORIZON_WEIGHTS.get(
        TaskHorizon(task.horizon) if isinstance(task.horizon, str) else task.horizon,
        1.0,
    )

    readiness = 1.0

    # Time decay (score rises slightly the longer a task waits, preventing starvation)
    age_hours = (now - task.created_at).total_seconds() / 3600
    age_boost = 1.0 + min(age_hours / 168, 0.5)

    # Pinned tag boosts task to the top
    pinned_boost = 1000.0 if "pinned" in (task.tags or []) else 0.0

    return priority_w * horizon_w * readiness * age_boost + pinned_boost


def backlog_sort_key(task: Task, now: datetime) -> tuple[float, datetime]:
    """Pending order: score high to low, and on equal scores the task put up first.

    The wait boost caps at 3.5 days, so every task older than that in one
    priority x horizon cell scores the same; the tie used to fall back to the
    repository's newest-first order, which put the oldest last.
    """
    return (-calculate_task_score(task, now), task.created_at)


# ── Task-wall digest ──────────────────────────────────────────────────────────

OPEN_STATUSES = (
    TaskStatus.PENDING.value,
    TaskStatus.RUNNING.value,
    TaskStatus.BLOCKED.value,
    TaskStatus.FAILED.value,
)
HORIZONS = (TaskHorizon.SHORT.value, TaskHorizon.MID.value, TaskHorizon.LONG.value)
PRIORITIES = tuple(p.value for p in TaskPriority)

# A running task with no work action for this long is stale; a pending one this
# long untouched is dormant. The running gap is measured: 170 completed tasks never
# went quiet for more than 5.6 days while running, and the open ones on 2026-09-29
# split into 0-6 days and 14+ days.
STALE_RUNNING_DAYS = 7
DORMANT_PENDING_DAYS = 30

# Memory reconciliation writes its merged memo under this author and invalidates
# the originals: the merge is not work on the task, the originals were.
RECONCILE_AUTHOR = "reconcile"

# How far back every memo is read. Past it, one latest memo per task stands in
# (repository.list_task_activity): longer than the dormant threshold, so an older
# memo can only ever mean "quiet for longer than any threshold" and every stale and
# dormant decision is made on complete data, while the read stays bounded by the
# recent writing rate instead of the whole history.
ACTIVITY_WINDOW_DAYS = 35
# Completed tasks are read from their own row; only the ones closed this recently
# also bring their status changes (the "last 7 days" the digest reports).
CLOSED_WINDOW_DAYS = 7

DIGEST_TOP = 5
DIGEST_RECENT = 5
DIGEST_TEXT_MAX_CHARS = 1200
DIGEST_TITLE_CHARS = 30

_DAY = 86400.0
_STATUS_KIND: dict[str, TaskActivityKind] = {
    TaskStatus.RUNNING.value: "started",
    TaskStatus.BLOCKED.value: "blocked",
    TaskStatus.PENDING.value: "reopened",
    TaskStatus.FAILED.value: "failed",
    TaskStatus.COMPLETED.value: "closed",
}
# Equal timestamps: the later step of a task's life wins (a task created running).
_KIND_RANK = {
    "created": 0, "started": 1, "memo": 2, "subtask": 2, "reopened": 3, "blocked": 3, "failed": 3, "closed": 4,
}
# A task's own timestamps within this of its creation mean it was created in that status.
_CREATED_IN_STATUS = timedelta(seconds=1)


@dataclass(frozen=True)
class WallActivity:
    """Per-task activity for every top-level task."""

    items: dict[str, DigestItem]


def _value(field: object) -> str:
    return str(getattr(field, "value", field) or "")


def _days(delta: timedelta) -> float:
    return max(delta.total_seconds(), 0.0) / _DAY


def _created_status(task: Task) -> str:
    """The status a task was created in: pending, unless its own timestamps say otherwise.

    No event records the creation, so the first status change has nothing to compare
    with; without this a rewrite of "pending" on a new task read as "reopened".
    """
    for stamp, status in ((task.completed_at, TaskStatus.COMPLETED.value), (task.started_at, TaskStatus.RUNNING.value)):
        if stamp is not None and abs(stamp - task.created_at) < _CREATED_IN_STATUS:
            return status
    return TaskStatus.PENDING.value


def summarize_activity(
    tasks: Iterable[Task],
    records: Iterable[TaskActivityRecord],
    now: datetime,
    *,
    stale_days: int = STALE_RUNNING_DAYS,
) -> WallActivity:
    """Each top-level task's latest work action, its idle and wall days.

    Work actions: the task's own created/started/completed timestamps, each status
    transition, each status change of one of its subtasks, and each memo on it or
    on a subtask except memory reconciliation's merges. Every such memo counts,
    however many tasks its author wrote on at the same time (founder, 2026-09-29:
    no batch-review rule). A completed task is described by its own row: its
    memos are not its activity (the repository does not even read them).
    Invalidated memos still count: writing one was an action. A status record that
    repeats the task's previous status is a rewrite, not a transition; before the
    first record the previous status is the one the task was created in.
    Editing a title, description, priority, horizon or config is never activity.
    """
    top_level = [task for task in tasks if not task.parent_id]
    ids = {task.id for task in top_level}
    memos: list[TaskActivityRecord] = []
    statuses: dict[str, list[TaskActivityRecord]] = defaultdict(list)
    last_subtask: dict[str, TaskActivityRecord] = {}
    for record in records:
        if record.task_id not in ids:
            continue
        if record.source == "memo":
            if record.author != RECONCILE_AUTHOR:
                memos.append(record)
        elif record.source == "subtask":
            seen = last_subtask.get(record.task_id)
            if seen is None or record.at >= seen.at:
                last_subtask[record.task_id] = record
        else:
            statuses[record.task_id].append(record)

    closed = {task.id for task in top_level if _value(task.status) == TaskStatus.COMPLETED.value}
    last_memo: dict[str, TaskActivityRecord] = {}
    for memo in memos:
        if memo.task_id in closed:
            continue
        seen = last_memo.get(memo.task_id)
        if seen is None or memo.at >= seen.at:
            last_memo[memo.task_id] = memo

    created_status = {task.id: _created_status(task) for task in top_level}
    last_transition: dict[str, TaskActivityRecord] = {}
    blocked_since: dict[str, datetime] = {}
    for task_id, changes in statuses.items():
        changes.sort(key=lambda change: change.at)
        previous = created_status[task_id]
        for change in changes:
            if change.status and change.status != previous:
                last_transition[task_id] = change
                if change.status == TaskStatus.BLOCKED.value:
                    blocked_since[task_id] = change.at
            previous = change.status or previous

    items: dict[str, DigestItem] = {}
    for task in top_level:
        # (time, rank, kind, memo type, author)
        candidates: list[tuple[datetime, int, TaskActivityKind, str, str]] = [
            (task.created_at, _KIND_RANK["created"], "created", "", ""),
        ]
        if task.started_at:
            candidates.append((task.started_at, _KIND_RANK["started"], "started", "", ""))
        if task.completed_at:
            candidates.append((task.completed_at, _KIND_RANK["closed"], "closed", "", ""))
        change = last_transition.get(task.id)
        if change is not None:
            kind = _STATUS_KIND.get(change.status, "started")
            candidates.append((change.at, _KIND_RANK[kind], kind, "", ""))
        memo = last_memo.get(task.id)
        if memo is not None:
            candidates.append((memo.at, _KIND_RANK["memo"], "memo", memo.memo_type, memo.author))
        subtask = last_subtask.get(task.id)
        if subtask is not None:
            candidates.append((subtask.at, _KIND_RANK["subtask"], "subtask", "", ""))
        at, _, kind, memo_type, author = max(candidates, key=lambda c: (c[0], c[1]))
        status = _value(task.status)
        idle = _days(now - at)
        blocked_days = None
        if status == TaskStatus.BLOCKED.value:
            blocked_days = _days(now - blocked_since.get(task.id, task.created_at))
        items[task.id] = DigestItem(
            id=task.id,
            title=task.title,
            status=status,
            priority=_value(task.priority),
            horizon=_value(task.horizon),
            created_at=task.created_at,
            activity_at=at,
            activity_kind=kind,
            activity_memo_type=memo_type,
            activity_by=author,
            idle_days=idle,
            wall_days=_days(now - task.created_at),
            stale=status == TaskStatus.RUNNING.value and idle >= stale_days,
            blocked_days=blocked_days,
        )
    return WallActivity(items=items)


def build_task_wall_digest(
    project_id: str,
    tasks: Iterable[Task],
    records: Iterable[TaskActivityRecord],
    now: datetime,
    *,
    top_n: int = DIGEST_TOP,
    recent_n: int = DIGEST_RECENT,
    stale_days: int = STALE_RUNNING_DAYS,
    dormant_days: int = DORMANT_PENDING_DAYS,
    activity: WallActivity | None = None,
) -> TaskWallDigest:
    """The task-wall digest of one project at ``now``. Pure: no IO, no clock.

    Scope: the project's top-level tasks. Open = pending, running, blocked, failed;
    completed tasks only feed completed_total, closed_7d and the recent list.
    - recent: every top-level task, completed included, by latest work action.
    - top: pending by backlog_sort_key (the existing score; ties go to the task
      put up first).
    - heads: the first pending task of the mid and of the long horizon that is not
      already in top; a horizon without one has no entry.
    - stuck: blocked (longest blocked first), failed, then stale running (longest
      idle first).
    ``activity`` lets a caller that already summarized the same inputs reuse it.
    """
    tasks = [task for task in tasks if not task.parent_id]
    if activity is None:
        activity = summarize_activity(tasks, records, now, stale_days=stale_days)
    items = activity.items

    by_status = dict.fromkeys(OPEN_STATUSES, 0)
    by_horizon = dict.fromkeys(HORIZONS, 0)
    by_priority = dict.fromkeys(PRIORITIES, 0)
    matrix = {status: dict.fromkeys(HORIZONS, 0) for status in OPEN_STATUSES}
    open_tasks: list[Task] = []
    week_ago = now - timedelta(days=CLOSED_WINDOW_DAYS)
    created_7d = closed_7d = completed_total = 0
    for task in tasks:
        status, horizon = _value(task.status), _value(task.horizon)
        if task.created_at >= week_ago:
            created_7d += 1
        if status == TaskStatus.COMPLETED.value:
            completed_total += 1
            if task.completed_at and task.completed_at >= week_ago:
                closed_7d += 1
            continue
        if status not in by_status:
            continue
        open_tasks.append(task)
        by_status[status] += 1
        by_horizon[horizon] = by_horizon.get(horizon, 0) + 1
        by_priority[_value(task.priority)] = by_priority.get(_value(task.priority), 0) + 1
        matrix[status][horizon] = matrix[status].get(horizon, 0) + 1

    open_items = [items[task.id] for task in open_tasks]
    blocked = [item for item in open_items if item.status == TaskStatus.BLOCKED.value]
    failed = [item for item in open_items if item.status == TaskStatus.FAILED.value]
    stale = [item for item in open_items if item.stale]
    dormant = [
        item for item in open_items
        if item.status == TaskStatus.PENDING.value and item.idle_days >= dormant_days
    ]

    recent = sorted(items.values(), key=lambda item: (item.activity_at, item.id), reverse=True)[:recent_n]
    pending = sorted(
        (task for task in open_tasks if _value(task.status) == TaskStatus.PENDING.value),
        key=lambda task: backlog_sort_key(task, now),
    )
    top = [items[task.id] for task in pending[:top_n]]
    top_ids = {item.id for item in top}
    heads: dict[str, DigestItem] = {}
    for horizon in (TaskHorizon.MID.value, TaskHorizon.LONG.value):
        head = next(
            (task for task in pending if _value(task.horizon) == horizon and task.id not in top_ids),
            None,
        )
        if head is not None:
            heads[horizon] = items[head.id]

    return TaskWallDigest(
        project_id=project_id,
        as_of=now,
        open_total=len(open_tasks),
        by_status=by_status,
        by_horizon=by_horizon,
        matrix=matrix,
        by_priority=by_priority,
        created_7d=created_7d,
        closed_7d=closed_7d,
        completed_total=completed_total,
        stale_days=stale_days,
        stale_running=len(stale),
        dormant_days=dormant_days,
        dormant_pending=len(dormant),
        blocked_oldest_days=max((item.blocked_days or 0.0 for item in blocked), default=None),
        recent=recent,
        top=top,
        heads=heads,
        stuck=(
            sorted(blocked, key=lambda item: item.blocked_days or 0.0, reverse=True)
            + sorted(failed, key=lambda item: item.idle_days, reverse=True)
            + sorted(stale, key=lambda item: item.idle_days, reverse=True)
        ),
    )


# ── Digest text (the session briefing, task_list_project and /loop read this) ──

_STATUS_LABEL = {"pending": "待办", "running": "进行中", "blocked": "阻塞", "failed": "失败", "completed": "已关闭"}
_HORIZON_SHORT = {"short": "短", "mid": "中", "long": "长"}
_HORIZON_HEAD = {"mid": "中期", "long": "长期"}
_KIND_LABEL = {
    "created": "新建", "started": "开工", "blocked": "转阻塞", "reopened": "退回待办",
    "failed": "失败", "closed": "关闭", "subtask": "子任务",
}
_MEMO_LABEL = {"progress": "进展", "decision": "决策", "issue": "问题", "summary": "总结"}
_MATRIX_ORDER = ("running", "pending", "blocked", "failed")


def _ago(delta: timedelta) -> str:
    seconds = max(int(delta.total_seconds()), 0)
    if seconds < 60:
        return "刚刚"
    if seconds < 3600:
        return f"{seconds // 60}分钟前"
    if seconds < 86400:
        return f"{seconds // 3600}小时前"
    return f"{seconds // 86400}天前"


def _title(item: DigestItem, chars: int) -> str:
    title = clean_text(item.title) or "（无标题）"
    return title if len(title) <= chars else title[:chars].rstrip() + "…"


def _field(value: str) -> str:
    return clean_text(value)[:20]


def _kind(item: DigestItem) -> str:
    if item.activity_kind == "memo":
        return _MEMO_LABEL.get(item.activity_memo_type, "记录")
    return _KIND_LABEL[item.activity_kind]


def _render(digest: TaskWallDigest, *, title_chars: int, rows: int, heads: bool) -> str:
    now = digest.as_of
    recent = digest.recent[:rows]
    top = digest.top[:rows]
    head_items = [(h, digest.heads[h]) for h in ("mid", "long") if h in digest.heads] if heads else []
    listed = {item.id for item in (*recent, *top, *(item for _, item in head_items))
              if item.status != TaskStatus.COMPLETED.value}

    lines = [
        f"=== 任务墙：未关 {digest.open_total} 条，下列其中 {len(listed)} 条；"
        "全墙用 task_list_project（可按 status/horizon 筛） ==="
    ]
    parts = []
    for status in _MATRIX_ORDER:
        total = digest.by_status.get(status, 0)
        if not total:
            continue
        cells = "/".join(
            f"{_HORIZON_SHORT[h]}{n}" for h, n in digest.matrix.get(status, {}).items()
            if n and h in _HORIZON_SHORT
        )
        parts.append(f"{_STATUS_LABEL[status]} {total}（{cells}）")
    lines.append("  " + (" · ".join(parts) if parts else "墙上没有未关的任务"))
    stats = [f"近 7 天新建 {digest.created_7d}、关闭 {digest.closed_7d}"]
    if digest.stale_running:
        stats.append(f"进行中 {digest.stale_running} 条超 {digest.stale_days} 天没动静")
    if digest.blocked_oldest_days is not None:
        stats.append(f"阻塞最久 {int(digest.blocked_oldest_days)} 天")
    if digest.dormant_pending:
        stats.append(f"待办 {digest.dormant_pending} 条超 {digest.dormant_days} 天没人碰")
    lines.append("  " + " · ".join(stats))

    if recent:
        lines.append(f"最近动静 {len(recent)}：")
        for item in recent:
            status = _STATUS_LABEL.get(item.status) or _field(item.status)
            lines.append(
                f"  {_ago(now - item.activity_at)}·{_kind(item)} "
                f"[{_field(item.priority)}/{_field(item.horizon)}·{status}] "
                f"{_title(item, title_chars)} #{item.id[:8]}"
            )
    if top:
        lines.append(f"待办最优先 {len(top)}（现有打分，同分先上墙的在前）：")
        for item in top:
            lines.append(
                f"  [{_field(item.priority)}/{_field(item.horizon)}] {_title(item, title_chars)} "
                f"上墙{int(item.wall_days)}天 #{item.id[:8]}"
            )
    elif digest.open_total:
        lines.append("待办：无")
    if head_items:
        lines.append("中长期待办之首：" + " · ".join(
            f"{_HORIZON_HEAD[h]} [{_field(item.priority)}] {_title(item, title_chars)} "
            f"上墙{int(item.wall_days)}天 #{item.id[:8]}"
            for h, item in head_items
        ))
    return "\n".join(lines)


def render_digest_text(digest: TaskWallDigest, *, max_chars: int = DIGEST_TEXT_MAX_CHARS) -> str:
    """The digest as the Chinese text block injected into the Leader's context.

    Every quoted field is cleaned to one line. Lines use single spaces inside and
    a two-space indent, so the hook's own cleaning leaves them byte for byte. Stays
    within ``max_chars``: past it the mid/long line goes first, then titles shrink,
    then each list keeps three rows.
    """
    for title_chars, rows, heads in (
        (DIGEST_TITLE_CHARS, DIGEST_TOP, True),
        (DIGEST_TITLE_CHARS, DIGEST_TOP, False),
        (20, DIGEST_TOP, False),
        (20, 3, False),
    ):
        text = _render(digest, title_chars=title_chars, rows=rows, heads=heads)
        if len(text) <= max_chars:
            return text
    lines = text.split("\n")
    while len(lines) > 1 and len("\n".join(lines)) > max_chars:
        lines.pop()
    return "\n".join(lines)[:max_chars]


def digest_payload(digest: TaskWallDigest) -> dict[str, Any]:
    """JSON form served by the API: the digest plus its rendered text."""
    payload = digest.model_dump(mode="json")
    payload["text"] = render_digest_text(digest)
    return payload


async def load_task_wall_digest(
    repo: Any,
    project_id: str,
    *,
    tasks: Sequence[Task] | None = None,
    now: datetime | None = None,
) -> tuple[TaskWallDigest, WallActivity]:
    """Read the inputs and build the digest. Pass ``tasks`` when already loaded."""
    if tasks is None:
        tasks = await repo.list_wall_tasks(project_id)
    now = now or utc_now()
    records = await repo.list_task_activity(
        project_id,
        since=now - timedelta(days=ACTIVITY_WINDOW_DAYS),
        closed_since=now - timedelta(days=CLOSED_WINDOW_DAYS),
    )
    activity = summarize_activity(tasks, records, now)
    return build_task_wall_digest(project_id, tasks, records, now, activity=activity), activity


class TaskWallEngine:
    """Task wall projection — pure read model over the task repository."""

    def __init__(self, repo: Any) -> None:
        self._repo = repo

    async def get_task_wall(
        self,
        team_id: str,
        horizon: str = "",
        priority: str = "",
    ) -> dict[str, Any]:
        """Get the task wall view."""
        all_tasks = await self._repo.list_tasks(team_id)

        # Build parent_id → children mapping so subtasks can be nested into parent items.
        subtask_id_to_stage: dict[str, dict] = {}
        children_map: dict[str, list] = {}
        for task in all_tasks:
            if task.parent_id:
                children_map.setdefault(task.parent_id, []).append(task)
                subtask_id_to_stage[task.id] = {}

        # Populate stage metadata from parent pipeline configs.
        for task in all_tasks:
            pipeline_cfg = task.config.get("pipeline")
            if not pipeline_cfg:
                continue
            for stage in pipeline_cfg.get("stages", []):
                sid = stage.get("subtask_id")
                if sid and sid in subtask_id_to_stage:
                    subtask_id_to_stage[sid] = stage

        now = utc_now()
        # Calculate score and group by horizon
        wall: dict[str, list[dict]] = {"short": [], "mid": [], "long": []}
        completed_tasks: list[dict] = []

        for task in all_tasks:
            # Filter out pipeline subtasks — they have a parent_id and should not
            # appear as top-level cards on the task wall.
            if task.parent_id:
                continue

            if task.status == TaskStatus.COMPLETED:
                item_c = task.model_dump(mode="json")
                # Nest subtasks for completed parent tasks.
                child_tasks = children_map.get(task.id, [])
                nested_c: list[dict] = []
                for child in child_tasks:
                    stage_meta = subtask_id_to_stage.get(child.id, {})
                    child_status = child.status if isinstance(child.status, str) else child.status.value
                    nested_c.append({
                        "id": child.id,
                        "title": child.title,
                        "status": child_status,
                        "stage_name": stage_meta.get("name"),
                        "agent_template": stage_meta.get("agent_template"),
                        "completed_at": child.completed_at.isoformat() if child.completed_at else None,
                    })
                item_c["subtasks"] = nested_c
                completed_tasks.append(item_c)
                continue

            h = task.horizon if isinstance(task.horizon, str) else task.horizon.value
            if horizon and h != horizon:
                continue

            p = task.priority if isinstance(task.priority, str) else task.priority.value
            if priority and p not in priority.split(","):
                continue

            score = calculate_task_score(task, now)
            item = task.model_dump(mode="json")
            item["score"] = round(score, 1)
            item["_rank"] = backlog_sort_key(task, now)

            # Attach pipeline progress summary if the task has a pipeline config.
            pipeline_cfg = task.config.get("pipeline")
            if pipeline_cfg:
                stages = pipeline_cfg.get("stages", [])
                active = [s for s in stages if s.get("status") != "skipped"]
                done = [s for s in active if s.get("status") in ("completed", "skipped")]
                total_active = len(active)
                done_count = len(done)
                current_idx = pipeline_cfg.get("current_stage_index", 0)
                current_stage_name = None
                if current_idx < len(stages):
                    current_stage_name = stages[current_idx].get("name")
                pct = round(done_count / total_active * 100) if total_active > 0 else 0
                item["pipeline_progress"] = f"{done_count}/{total_active}"
                item["pipeline_current_stage"] = current_stage_name
                item["pipeline_pct"] = pct

            # Nest subtasks into parent item.
            child_tasks = children_map.get(task.id, [])
            nested: list[dict] = []
            for child in child_tasks:
                stage_meta = subtask_id_to_stage.get(child.id, {})
                child_status = child.status if isinstance(child.status, str) else child.status.value
                nested.append({
                    "id": child.id,
                    "title": child.title,
                    "status": child_status,
                    "stage_name": stage_meta.get("name"),
                    "agent_template": stage_meta.get("agent_template"),
                    "completed_at": child.completed_at.isoformat() if child.completed_at else None,
                })
            item["subtasks"] = nested

            if h in wall:
                wall[h].append(item)

        # Within each group: score descending, equal scores first up first (backlog_sort_key).
        for key in wall:
            wall[key].sort(key=lambda x: x["_rank"])
            for item in wall[key]:
                item.pop("_rank")

        # Sort completed tasks by completion time descending
        completed_tasks.sort(
            key=lambda x: x.get("completed_at") or "",
            reverse=True,
        )

        stats = {
            "total": len(all_tasks),
            "by_status": {},
            "completed_count": len(completed_tasks),
        }
        for task in all_tasks:
            s = task.status if isinstance(task.status, str) else task.status.value
            stats["by_status"][s] = stats["by_status"].get(s, 0) + 1

        return {"wall": wall, "completed": completed_tasks, "stats": stats}
