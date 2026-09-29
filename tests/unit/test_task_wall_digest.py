"""The task-wall digest's rules, pinned on the pure function with a fixed clock.

build_task_wall_digest is the one summary the session briefing, the compaction
briefing, task_list_project, /loop and the Dashboard read (design:
docs/task-wall-digest-design.md). Every rule the founder ruled on 2026-09-29 has
a case here: the stale and dormant thresholds at their edges, every memo counting
(no batch-review rule), memory reconciliation's merges, completed tasks described
by their own row, status rewrites, FIFO on equal scores, the recent list with
closed tasks, the mid/long heads, the counting scope, and the 1200-character text
budget.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from aiteam.loop.task_wall_engine import (
    DIGEST_TEXT_MAX_CHARS,
    backlog_sort_key,
    build_task_wall_digest,
    render_digest_text,
    summarize_activity,
)
from aiteam.types import Task, TaskActivityRecord, TaskHorizon, TaskPriority, TaskStatus

NOW = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)
PROJECT = "p-digest"


def _task(title: str, status: str = "pending", *, age: float = 1.0, priority: str = "medium",
          horizon: str = "short", **extra) -> Task:
    created = NOW - timedelta(days=age)
    if status == "completed":
        extra.setdefault("completed_at", created)
    return Task(title=title, status=TaskStatus(status), priority=TaskPriority(priority),
                horizon=TaskHorizon(horizon), project_id=PROJECT, created_at=created, **extra)


def _memo(task: Task, ago: timedelta, author: str = "worker", memo_type: str = "progress") -> TaskActivityRecord:
    return TaskActivityRecord(task_id=task.id, at=NOW - ago, source="memo", author=author,
                              memo_type=memo_type)


def _status(task: Task, ago: timedelta, status: str) -> TaskActivityRecord:
    return TaskActivityRecord(task_id=task.id, at=NOW - ago, source="status", status=status)


def _digest(tasks, records=(), **kw):
    return build_task_wall_digest(PROJECT, tasks, list(records), NOW, **kw)


# ── Stale and dormant thresholds ──────────────────────────────────────────────


@pytest.mark.parametrize(("idle_days", "stale"), [(6.99, False), (7.01, True)])
def test_running_goes_stale_after_seven_quiet_days(idle_days, stale):
    task = _task("running work", "running", age=40)
    digest = _digest([task], [_memo(task, timedelta(days=idle_days))])
    assert digest.stale_running == int(stale)
    assert [item.id for item in digest.stuck] == ([task.id] if stale else [])


@pytest.mark.parametrize(("idle_days", "dormant"), [(29.99, 0), (30.01, 1)])
def test_pending_goes_dormant_after_thirty_untouched_days(idle_days, dormant):
    task = _task("backlog item", age=idle_days)
    assert _digest([task]).dormant_pending == dormant


# ── What counts as activity ───────────────────────────────────────────────────


def test_every_memo_counts_however_many_tasks_its_author_touched():
    """Founder, 2026-09-29: no batch-review rule. One author noting ten tasks within a
    few minutes moves all ten; the known cost is that a checking pass resets the
    stale clock and fills the recent list."""
    tasks = [_task(f"t{i}", "running", age=30) for i in range(10)]
    notes = [_memo(task, timedelta(hours=1) - timedelta(minutes=i), author="auditor") for i, task in enumerate(tasks)]
    digest = _digest(tasks, notes)
    assert digest.stale_running == 0
    assert [(item.activity_kind, item.activity_by) for item in digest.recent] == [("memo", "auditor")] * 5
    assert "批量" not in render_digest_text(digest)


def test_a_closed_tasks_memo_is_not_its_activity():
    """A completed task is described by its own row: a wrap-up memo after closing
    leaves it at "closed" (the repository does not read such memos at all)."""
    closed = [_task(f"closed {i}", "completed", age=30, completed_at=NOW - timedelta(days=9)) for i in range(2)]
    digest = _digest(closed, [_memo(closed[0], timedelta(hours=2), memo_type="summary")])
    assert {item.activity_kind for item in digest.recent} == {"closed"}


def test_reconcile_merge_memo_is_not_activity_but_the_invalidated_original_is():
    task = _task("old task", "running", age=40)
    original = _memo(task, timedelta(days=20))  # later invalidated by the merge: still counts
    merge = _memo(task, timedelta(hours=1), author="reconcile", memo_type="summary")
    item = summarize_activity([task], [original, merge], NOW).items[task.id]
    assert item.activity_kind == "memo" and item.activity_by == "worker"
    assert round(item.idle_days) == 20


def test_status_rewrite_is_not_a_transition():
    task = _task("steady", "running", age=40, started_at=NOW - timedelta(days=30))
    records = [_status(task, timedelta(days=30), "running"), _status(task, timedelta(days=1), "running")]
    item = summarize_activity([task], records, NOW).items[task.id]
    assert round(item.idle_days) == 30 and item.activity_kind == "started"


def test_transitions_name_the_action_and_blocked_days_count_from_entering_blocked():
    task = _task("waits", "blocked", age=20)
    records = [_status(task, timedelta(days=15), "running"), _status(task, timedelta(days=9), "blocked"),
               _memo(task, timedelta(days=2))]
    digest = _digest([task], records)
    item = digest.stuck[0]
    assert item.activity_kind == "memo" and round(item.idle_days) == 2
    assert round(item.blocked_days) == 9 and round(digest.blocked_oldest_days) == 9
    back = [_status(task, timedelta(days=15), "running"), _status(task, timedelta(days=1), "pending")]
    assert summarize_activity([task], back, NOW).items[task.id].activity_kind == "reopened"


def test_a_subtask_moving_keeps_its_pipeline_parent_active():
    """The repository files a subtask's status change under its parent as source "subtask"."""
    parent = _task("pipeline", "running", age=40, started_at=NOW - timedelta(days=40))
    stage = TaskActivityRecord(task_id=parent.id, at=NOW - timedelta(hours=1), source="subtask", status="completed")
    digest = _digest([parent], [stage])
    assert digest.stale_running == 0 and digest.stuck == []
    assert digest.recent[0].activity_kind == "subtask"
    assert "1小时前·子任务 [medium/short·进行中] pipeline" in render_digest_text(digest)
    # A subtask's own transitions never become the parent's status or blocked time.
    blocked = _task("waits", "blocked", age=20)
    item = summarize_activity([blocked], [TaskActivityRecord(
        task_id=blocked.id, at=NOW - timedelta(days=1), source="subtask", status="blocked")], NOW).items[blocked.id]
    assert round(item.blocked_days) == 20


def test_a_first_status_record_repeating_the_created_status_is_a_rewrite():
    """No event records the creation: a task created pending that gets "pending"
    written again did not go back to the backlog."""
    task = _task("fresh", age=10)
    item = summarize_activity([task], [_status(task, timedelta(days=1), "pending")], NOW).items[task.id]
    assert (item.activity_kind, round(item.idle_days)) == ("created", 10)
    running = _task("created running", "running", age=10, started_at=NOW - timedelta(days=10))
    rewrite = summarize_activity([running], [_status(running, timedelta(days=1), "running")], NOW)
    assert (rewrite.items[running.id].activity_kind, round(rewrite.items[running.id].idle_days)) == ("started", 10)
    moved = summarize_activity([task], [_status(task, timedelta(days=1), "running")], NOW)
    assert moved.items[task.id].activity_kind == "started" and round(moved.items[task.id].idle_days) == 1


# ── Lists ─────────────────────────────────────────────────────────────────────


def test_top_keeps_the_score_and_breaks_ties_first_up_first():
    older, middle, newer = (_task(f"high {d}", age=d, priority="high") for d in (20, 10, 5))
    low = _task("low", age=30, priority="low")
    fresh = _task("fresh high", age=0.5, priority="high")
    digest = _digest([newer, low, older, fresh, middle])
    # 20, 10 and 5 days all sit at the 3.5-day cap: equal scores, the oldest first.
    assert [item.title for item in digest.top] == ["high 20", "high 10", "high 5", "fresh high", "low"]
    assert backlog_sort_key(older, NOW) < backlog_sort_key(middle, NOW)


def test_recent_takes_closed_tasks_and_labels_the_action():
    closed = _task("shipped", "completed", age=3, completed_at=NOW - timedelta(minutes=5))
    noted = _task("noted", "running", age=3)
    fresh = _task("fresh", age=0.01)
    old = _task("old", age=9)
    digest = _digest([old, closed, noted, fresh],
                     [_memo(noted, timedelta(minutes=30), memo_type="decision", author="lead")])
    kinds = [(item.title, item.activity_kind) for item in digest.recent]
    assert kinds == [("shipped", "closed"), ("fresh", "created"), ("noted", "memo"), ("old", "created")]
    assert digest.recent[2].activity_memo_type == "decision" and digest.recent[2].activity_by == "lead"
    assert "关闭 [medium/short·已关闭] shipped" in render_digest_text(digest)


def test_heads_skip_the_top_and_leave_an_empty_horizon_out():
    tasks = [_task(f"mid {i}", horizon="mid", priority="critical", age=10 + i) for i in range(6)]
    digest = _digest(tasks)
    assert [item.title for item in digest.top] == [f"mid {i}" for i in (5, 4, 3, 2, 1)]
    assert digest.heads["mid"].title == "mid 0"
    assert "long" not in digest.heads


def test_counts_cover_open_top_level_tasks_only():
    parent = _task("parent", "running", horizon="mid")
    tasks = [
        parent,
        _task("sub", "running", parent_id=parent.id),
        _task("p1"), _task("p2", horizon="long"),
        _task("b", "blocked"), _task("f", "failed"),
        _task("done recently", "completed", age=2),
        _task("done long ago", "completed", age=40),
    ]
    digest = _digest(tasks)
    assert digest.open_total == 5
    assert digest.by_status == {"pending": 2, "running": 1, "blocked": 1, "failed": 1}
    assert digest.matrix["pending"] == {"short": 1, "mid": 0, "long": 1}
    assert digest.by_horizon == {"short": 3, "mid": 1, "long": 1}
    assert (digest.completed_total, digest.closed_7d, digest.created_7d) == (2, 1, 6)


# ── Text ──────────────────────────────────────────────────────────────────────


def _real_scale_wall() -> tuple[list[Task], list[TaskActivityRecord]]:
    """The 2026-09-29 shape: 82 open, 57 pending, 21 running, 4 blocked, 212 closed, long titles."""
    title = "任务墙摘要样例标题：一个足够长的中性标题，用来占满渲染预算并且检查截断是否生效，" * 2
    tasks: list[Task] = []
    for i in range(57):
        tasks.append(_task(f"{i}{title}", age=i % 60 + 0.5, priority=("high", "medium", "low")[i % 3],
                           horizon=("short", "mid", "long")[i % 3]))
    tasks += [_task(f"r{i}{title}", "running", age=i + 1, horizon=("short", "mid")[i % 2]) for i in range(21)]
    tasks += [_task(f"b{i}{title}", "blocked", age=i + 5) for i in range(4)]
    tasks += [_task(f"c{i}{title}", "completed", age=i % 90 + 0.1) for i in range(212)]
    records = [_memo(task, timedelta(minutes=7 * i + 3), author=f"a{i % 3}", memo_type="decision")
               for i, task in enumerate(tasks[57:78])]
    records += [_memo(task, timedelta(minutes=2 + i), author="auditor") for i, task in enumerate(tasks[:9])]
    return tasks, records


def test_text_stays_within_budget_at_real_scale_and_lines_survive_the_hook_cleaning():
    tasks, records = _real_scale_wall()
    digest = _digest(tasks, records)
    assert (digest.open_total, digest.completed_total) == (82, 212)
    text = render_digest_text(digest)
    assert len(text) <= DIGEST_TEXT_MAX_CHARS
    lines = text.split("\n")
    assert lines[0].startswith("=== 任务墙：未关 82 条")
    assert lines[1] == "  进行中 21（短11/中10） · 待办 57（短19/中19/长19） · 阻塞 4（短4）"
    assert "最近动静 5：" in lines and any(line.startswith("中长期待办之首：") for line in lines)
    for line in lines:
        body = line.lstrip(" ")
        assert body == " ".join(body.split()), f"not a single-spaced line: {line!r}"
        assert len(line) - len(body) in (0, 2)


def test_text_gives_up_the_heads_line_before_anything_else():
    tasks, records = _real_scale_wall()
    digest = _digest(tasks, records)
    full = render_digest_text(digest)
    assert "中长期待办之首" in full
    squeezed = render_digest_text(digest, max_chars=len(full) - 1)
    assert "中长期待办之首" not in squeezed
    assert "最近动静 5：" in squeezed and "待办最优先 5" in squeezed
    # Only the heads line went (the header's listed count drops with it).
    assert squeezed.split("\n")[1:] == full.split("\n")[1:-1]


def test_empty_wall_renders_a_short_honest_block():
    text = render_digest_text(_digest([]))
    assert text.split("\n")[:2] == [
        "=== 任务墙：未关 0 条，下列其中 0 条；全墙用 task_list_project（可按 status/horizon 筛） ===",
        "  墙上没有未关的任务",
    ]
