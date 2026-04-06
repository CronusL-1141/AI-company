"""Execution replay engine — step through task execution history."""

from __future__ import annotations

from typing import Any

from aiteam.storage.repository import StorageRepository


class ReplayEngine:
    def __init__(self, repo: StorageRepository):
        self._repo = repo

    async def get_replay(self, task_id: str) -> dict[str, Any]:
        """Build complete replay data for a task.

        Collects task info, all memo records (including subtasks), and
        lifecycle events into a unified chronological timeline with
        checkpoint markers at summary/decision memos.

        Returns:
            task: Task details
            timeline: Chronological list of replay steps
            checkpoints: Summary/decision memos only (key decision points)
            stats: Execution statistics (duration, memo counts, etc.)
        """
        task = await self._repo.get_task(task_id)
        if not task:
            return {"error": f"task {task_id} not found"}

        timeline: list[dict[str, Any]] = []

        # --- Task lifecycle events ---
        if task.created_at:
            timeline.append(
                {
                    "timestamp": task.created_at.isoformat(),
                    "step_type": "lifecycle",
                    "event": "task_created",
                    "author": task.assigned_to or "system",
                    "content": f"任务创建: {task.title}",
                    "task_id": task_id,
                    "source": "main_task",
                }
            )
        if task.started_at:
            timeline.append(
                {
                    "timestamp": task.started_at.isoformat(),
                    "step_type": "lifecycle",
                    "event": "task_started",
                    "author": task.assigned_to or "system",
                    "content": "任务开始执行",
                    "task_id": task_id,
                    "source": "main_task",
                }
            )

        # --- Main task memos ---
        main_memos: list[dict[str, Any]] = task.config.get("memo", [])
        for memo in main_memos:
            timeline.append(
                {
                    "timestamp": memo.get("timestamp", ""),
                    "step_type": "memo",
                    "event": memo.get("type", "progress"),
                    "author": memo.get("author", ""),
                    "content": memo.get("content", ""),
                    "task_id": task_id,
                    "source": "main_task",
                }
            )

        # --- Subtask memos ---
        all_tasks = await self._repo.list_tasks(task.team_id) if task.team_id else []
        subtasks = [t for t in all_tasks if t.parent_id == task_id]
        subtasks.sort(key=lambda t: t.order)

        for subtask in subtasks:
            # Subtask lifecycle events
            if subtask.created_at:
                timeline.append(
                    {
                        "timestamp": subtask.created_at.isoformat(),
                        "step_type": "lifecycle",
                        "event": "subtask_created",
                        "author": subtask.assigned_to or "system",
                        "content": f"子任务创建: {subtask.title}",
                        "task_id": subtask.id,
                        "source": "subtask",
                    }
                )

            # Subtask memos
            subtask_memos: list[dict[str, Any]] = subtask.config.get("memo", [])
            for memo in subtask_memos:
                timeline.append(
                    {
                        "timestamp": memo.get("timestamp", ""),
                        "step_type": "memo",
                        "event": memo.get("type", "progress"),
                        "author": memo.get("author", ""),
                        "content": memo.get("content", ""),
                        "task_id": subtask.id,
                        "source": "subtask",
                    }
                )

        # --- Task completed event ---
        if task.completed_at:
            timeline.append(
                {
                    "timestamp": task.completed_at.isoformat(),
                    "step_type": "lifecycle",
                    "event": "task_completed",
                    "author": task.assigned_to or "system",
                    "content": "任务完成",
                    "task_id": task_id,
                    "source": "main_task",
                }
            )

        # Sort by timestamp, filter out entries with empty timestamps
        timeline = [e for e in timeline if e.get("timestamp")]
        timeline.sort(key=lambda x: x["timestamp"])

        # Assign sequential step numbers
        for i, step in enumerate(timeline):
            step["step"] = i + 1

        # Extract checkpoints: summary and decision memos
        checkpoints = [
            s for s in timeline if s.get("event") in ("summary", "decision")
        ]

        # Build execution statistics
        duration_seconds: float | None = None
        if task.started_at and task.completed_at:
            delta = task.completed_at - task.started_at
            duration_seconds = delta.total_seconds()

        memo_counts: dict[str, int] = {}
        for step in timeline:
            if step["step_type"] == "memo":
                key = step["event"]
                memo_counts[key] = memo_counts.get(key, 0) + 1

        stats: dict[str, Any] = {
            "total_steps": len(timeline),
            "total_checkpoints": len(checkpoints),
            "total_subtasks": len(subtasks),
            "memo_counts": memo_counts,
            "duration_seconds": duration_seconds,
            "status": task.status.value if hasattr(task.status, "value") else str(task.status),
            "assigned_to": task.assigned_to,
        }

        return {
            "task": task.model_dump(mode="json"),
            "timeline": timeline,
            "checkpoints": checkpoints,
            "stats": stats,
        }

    async def compare_executions(self, task_id_1: str, task_id_2: str) -> dict[str, Any]:
        """Compare two task executions side by side.

        Fetches full replay for both tasks and produces a diff-style comparison
        highlighting differences in duration, checkpoint count, and authors involved.

        Returns:
            task_1: replay data for first task
            task_2: replay data for second task
            diff: high-level comparison metrics
        """
        replay_1 = await self.get_replay(task_id_1)
        replay_2 = await self.get_replay(task_id_2)

        if "error" in replay_1:
            return {"error": f"task_1: {replay_1['error']}"}
        if "error" in replay_2:
            return {"error": f"task_2: {replay_2['error']}"}

        stats_1 = replay_1["stats"]
        stats_2 = replay_2["stats"]

        # Authors involved in each execution
        authors_1 = {s["author"] for s in replay_1["timeline"] if s.get("author")}
        authors_2 = {s["author"] for s in replay_2["timeline"] if s.get("author")}

        diff: dict[str, Any] = {
            "total_steps": {
                "task_1": stats_1["total_steps"],
                "task_2": stats_2["total_steps"],
                "delta": stats_2["total_steps"] - stats_1["total_steps"],
            },
            "total_checkpoints": {
                "task_1": stats_1["total_checkpoints"],
                "task_2": stats_2["total_checkpoints"],
                "delta": stats_2["total_checkpoints"] - stats_1["total_checkpoints"],
            },
            "total_subtasks": {
                "task_1": stats_1["total_subtasks"],
                "task_2": stats_2["total_subtasks"],
                "delta": stats_2["total_subtasks"] - stats_1["total_subtasks"],
            },
            "duration_seconds": {
                "task_1": stats_1["duration_seconds"],
                "task_2": stats_2["duration_seconds"],
                "delta": (
                    (stats_2["duration_seconds"] or 0) - (stats_1["duration_seconds"] or 0)
                    if stats_1["duration_seconds"] is not None
                    and stats_2["duration_seconds"] is not None
                    else None
                ),
            },
            "authors": {
                "task_1": sorted(authors_1),
                "task_2": sorted(authors_2),
                "only_in_task_1": sorted(authors_1 - authors_2),
                "only_in_task_2": sorted(authors_2 - authors_1),
                "shared": sorted(authors_1 & authors_2),
            },
            "status": {
                "task_1": stats_1["status"],
                "task_2": stats_2["status"],
            },
        }

        return {
            "task_1": replay_1,
            "task_2": replay_2,
            "diff": diff,
        }
