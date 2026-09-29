"""What the task-wall digest reads from storage: bounded, and subtasks count for their parent.

repository.list_task_activity feeds build_task_wall_digest. Pinned here on a real
(in-memory) database:
- a pipeline parent stays running while its subtasks carry the work, so a
  subtask's memo and status change must count as the parent's activity, or the
  parent reads as stale (L2 review, 2026-09-29);
- the read is bounded (founder, 2026-09-29): only open tasks are read. An open
  task's memos before the window come back as its latest few rows; no memo of a
  completed task is ever read; status changes come from open tasks and tasks
  closed within the last 7 days.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import update

from aiteam.clock import utc_now
from aiteam.loop.task_wall_engine import (
    ACTIVITY_WINDOW_DAYS,
    CLOSED_WINDOW_DAYS,
    DORMANT_PENDING_DAYS,
    build_task_wall_digest,
)
from aiteam.storage.connection import get_session
from aiteam.storage.models import TaskMemoModel
from aiteam.types import TaskStatus


def _windows():
    now = utc_now()
    return {
        "since": now - timedelta(days=ACTIVITY_WINDOW_DAYS),
        "closed_since": now - timedelta(days=CLOSED_WINDOW_DAYS),
    }


async def _age_memo(repo, memo_id: str, days: float) -> None:
    async with get_session(repo._db_url) as session:
        await session.execute(
            update(TaskMemoModel).where(TaskMemoModel.id == memo_id).values(created_at=utc_now() - timedelta(days=days))
        )


def test_the_window_outlasts_every_threshold():
    """Past the window only one memo per task is read: it must mean "quiet longer than any threshold"."""
    assert ACTIVITY_WINDOW_DAYS > DORMANT_PENDING_DAYS


@pytest.mark.asyncio
async def test_subtask_work_moves_the_parent(db_repository):
    repo = db_repository
    project = await repo.create_project(name="pipeline", root_path="/tmp/pipeline")
    now = utc_now()
    parent = await repo.create_task(team_id=None, title="pipeline parent", project_id=project.id, status="running")
    await repo.update_task(parent.id, created_at=now - timedelta(days=40), started_at=now - timedelta(days=40))
    child = await repo.create_task(team_id=None, title="stage 2", project_id=project.id, parent_id=parent.id)
    await repo.add_task_memo(child.id, "stage 2 half done", author="stage-worker", project_id=project.id)
    await repo.update_task(child.id, status=TaskStatus.RUNNING)

    records = await repo.list_task_activity(project.id, **_windows())
    assert {(r.task_id, r.source) for r in records} == {(parent.id, "memo"), (parent.id, "subtask")}

    tasks = await repo.list_wall_tasks(project.id)
    digest = build_task_wall_digest(project.id, tasks, records, utc_now())
    assert digest.stale_running == 0 and digest.stuck == []
    assert [item.id for item in digest.recent] == [parent.id]
    assert digest.recent[0].activity_kind == "subtask" and digest.recent[0].idle_days < 0.01


@pytest.mark.asyncio
async def test_an_open_tasks_history_before_the_window_is_its_latest_few_memos(db_repository):
    repo = db_repository
    project = await repo.create_project(name="history", root_path="/tmp/history")
    task = await repo.create_task(team_id=None, title="long history", project_id=project.id)
    for age in range(60, 80):
        memo = await repo.add_task_memo(task.id, f"old {age}", author="w", project_id=project.id)
        await _age_memo(repo, memo.id, age)
    # Newer than the others but not work: memory reconciliation's merge.
    merge = await repo.add_task_memo(task.id, "merged", author="reconcile", project_id=project.id)
    await _age_memo(repo, merge.id, 50)
    recent = await repo.add_task_memo(task.id, "new", author="w", memo_type="decision", project_id=project.id)
    await _age_memo(repo, recent.id, 2)

    memos = [r for r in await repo.list_task_activity(project.id, **_windows()) if r.source == "memo"]
    ages = sorted(round((utc_now() - r.at).total_seconds() / 86400) for r in memos)
    assert ages == [2, 60, 61], "the window, then the latest 3 before it minus the reconcile merge"
    assert max(memos, key=lambda r: r.at).memo_type == "decision"


@pytest.mark.asyncio
async def test_a_completed_task_brings_no_history(db_repository):
    repo = db_repository
    project = await repo.create_project(name="closed", root_path="/tmp/closed")
    now = utc_now()
    old = await repo.create_task(team_id=None, title="closed long ago", project_id=project.id)
    for age in range(40, 70):
        memo = await repo.add_task_memo(old.id, f"note {age}", author="w", project_id=project.id)
        await _age_memo(repo, memo.id, age)
    await repo.update_task(old.id, status=TaskStatus.COMPLETED, created_at=now - timedelta(days=80),
                           completed_at=now - timedelta(days=20))
    fresh = await repo.create_task(team_id=None, title="closed yesterday", project_id=project.id)
    note = await repo.add_task_memo(fresh.id, "wrap-up", author="w", memo_type="summary", project_id=project.id)
    await _age_memo(repo, note.id, 0.5)
    await repo.update_task(fresh.id, status=TaskStatus.COMPLETED, created_at=now - timedelta(days=10),
                           completed_at=now - timedelta(days=1))

    records = await repo.list_task_activity(project.id, **_windows())
    assert {(r.task_id, r.source) for r in records} == {(fresh.id, "status")}, (
        "no memo of a completed task (no open task wrote nearby), no status change of the one closed 20 days ago")
    digest = build_task_wall_digest(project.id, await repo.list_wall_tasks(project.id), records, utc_now())
    assert [(item.id, item.activity_kind) for item in digest.recent] == [(fresh.id, "closed"), (old.id, "closed")]
    assert (digest.closed_7d, digest.completed_total) == (1, 2)


@pytest.mark.asyncio
async def test_no_memo_of_a_completed_task_is_read_even_beside_an_open_ones(db_repository):
    """One author noting three open and two closed tasks within minutes: the open
    ones all move (every memo counts), the closed ones' memos are not read at all."""
    repo = db_repository
    project = await repo.create_project(name="batch", root_path="/tmp/batch")
    now = utc_now()
    opened = []
    for i in range(3):
        task = await repo.create_task(team_id=None, title=f"open {i}", project_id=project.id, status="running")
        await repo.update_task(task.id, created_at=now - timedelta(days=30), started_at=now - timedelta(days=30))
        memo = await repo.add_task_memo(task.id, "checked", author="auditor", project_id=project.id)
        await _age_memo(repo, memo.id, (60 + i) / 1440)
        opened.append(task)
    for i in range(2):
        task = await repo.create_task(team_id=None, title=f"closed {i}", project_id=project.id)
        await repo.update_task(task.id, status=TaskStatus.COMPLETED, created_at=now - timedelta(days=30),
                               completed_at=now - timedelta(days=20))
        memo = await repo.add_task_memo(task.id, "checked", author="auditor", project_id=project.id)
        await _age_memo(repo, memo.id, (63 + i) / 1440)

    records = await repo.list_task_activity(project.id, **_windows())
    assert sorted(r.task_id for r in records if r.source == "memo") == sorted(t.id for t in opened)
    digest = build_task_wall_digest(project.id, await repo.list_wall_tasks(project.id), records, utc_now())
    assert digest.stale_running == 0
