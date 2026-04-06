"""AI Team OS — Task pipeline orchestration.

PipelineManager creates and manages stage pipelines for tasks.
Each pipeline is a sequence of stages stored in task.config["pipeline"].
Stages are linked to child subtasks via depends_on chains.

Design: pure rule-driven state machine, no LLM dependency.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from aiteam.storage.repository import StorageRepository
from aiteam.types import TaskStatus

logger = logging.getLogger(__name__)

# ============================================================
# Pipeline template definitions
# ============================================================

PIPELINE_TEMPLATES: dict[str, list[dict[str, str]]] = {
    "feature": [
        {"name": "research", "agent_template": "explore-agent"},
        {"name": "design", "agent_template": "software-architect", "mode": "meeting", "meeting_template": "brainstorm"},
        {"name": "implement", "agent_template": "backend-architect"},
        {"name": "review", "agent_template": "code-reviewer", "mode": "meeting", "meeting_template": "council"},
        {"name": "test", "agent_template": "qa-engineer"},
        {"name": "deploy", "agent_template": "devops-engineer"},
    ],
    "bugfix": [
        {"name": "reproduce", "agent_template": "qa-engineer"},
        {"name": "diagnose", "agent_template": "backend-architect", "mode": "meeting", "meeting_template": "debate"},
        {"name": "fix", "agent_template": "backend-architect"},
        {"name": "review", "agent_template": "code-reviewer", "mode": "meeting", "meeting_template": "review"},
        # parallel_with example: test can run alongside fix when review triggers a rollback
        {"name": "test", "agent_template": "qa-engineer", "parallel_with": ["fix"]},
    ],
    "research": [
        {"name": "survey", "agent_template": "explore-agent"},
        {"name": "analyze", "agent_template": "software-architect", "mode": "meeting", "meeting_template": "decision"},
        {"name": "report", "agent_template": "technical-writer"},
        {"name": "review", "agent_template": "code-reviewer", "mode": "meeting", "meeting_template": "council"},
    ],
    "refactor": [
        {"name": "analysis", "agent_template": "software-architect", "mode": "meeting", "meeting_template": "council"},
        {"name": "plan", "agent_template": "software-architect", "mode": "meeting", "meeting_template": "decision"},
        {"name": "implement", "agent_template": "backend-architect"},
        {"name": "review", "agent_template": "code-reviewer", "mode": "meeting", "meeting_template": "review"},
        {"name": "test", "agent_template": "qa-engineer"},
    ],
}

# Shortcut pipelines (subsets of full pipelines)
SHORTCUT_PIPELINES: dict[str, list[dict[str, str]]] = {
    "quick-fix": [
        {"name": "implement", "agent_template": "backend-architect"},
        {"name": "test", "agent_template": "qa-engineer"},
    ],
    "spike": [
        {"name": "research", "agent_template": "explore-agent"},
        {"name": "report", "agent_template": "technical-writer"},
    ],
    "hotfix": [
        {"name": "fix", "agent_template": "backend-architect"},
        {"name": "test", "agent_template": "qa-engineer"},
    ],
}

# Review/Test failure rollback targets
ROLLBACK_MAP: dict[str, dict[str, str]] = {
    "feature": {"review": "implement", "test": "implement"},
    "bugfix": {"review": "fix", "test": "fix"},
    "research": {"review": "report"},
    "refactor": {"review": "implement", "test": "implement"},
    "quick-fix": {"test": "implement"},
    "hotfix": {"test": "fix"},
}

# Stage status constants
STAGE_PENDING = "pending"
STAGE_RUNNING = "running"
STAGE_COMPLETED = "completed"
STAGE_FAILED = "failed"
STAGE_SKIPPED = "skipped"

# Max rollback count before escalation
MAX_ROLLBACK_COUNT = 2


def _all_templates() -> dict[str, list[dict[str, str]]]:
    """Return combined pipeline + shortcut templates."""
    return {**PIPELINE_TEMPLATES, **SHORTCUT_PIPELINES}


class PipelineManager:
    """Pipeline manager — create, advance, fail, skip, and query pipelines.

    Pure rule-driven, no LLM dependency.
    """

    def __init__(self, repo: StorageRepository) -> None:
        self._repo = repo

    # ----------------------------------------------------------------
    # Create
    # ----------------------------------------------------------------

    async def create_pipeline(
        self,
        task_id: str,
        pipeline_type: str,
        skip_stages: list[str] | None = None,
    ) -> dict[str, Any]:
        """Create a pipeline for a task and generate chained subtasks.

        Args:
            task_id: Parent task ID.
            pipeline_type: Pipeline type key (feature/bugfix/research/refactor/quick-fix/spike/hotfix).
            skip_stages: Stage names to skip (optional).

        Returns:
            Pipeline overview including stages and subtask IDs.
        """
        templates = _all_templates()
        if pipeline_type not in templates:
            return {
                "success": False,
                "error": f"未知的 pipeline 类型: '{pipeline_type}'，"
                f"可选: {', '.join(templates.keys())}",
            }

        task = await self._repo.get_task(task_id)
        if task is None:
            return {"success": False, "error": f"任务 {task_id} 不存在"}

        # Check if pipeline already exists
        if task.config.get("pipeline"):
            return {"success": False, "error": "任务已有 pipeline，不能重复创建"}

        skip_set = set(skip_stages or [])
        stage_defs = templates[pipeline_type]

        # Build stages metadata
        stages: list[dict[str, Any]] = []
        for i, sdef in enumerate(stage_defs):
            stage_name = sdef["name"]
            status = STAGE_SKIPPED if stage_name in skip_set else STAGE_PENDING
            stage_entry: dict[str, Any] = {
                "name": stage_name,
                "status": status,
                "agent_template": sdef["agent_template"],
                "mode": sdef.get("mode", "agent"),
                "assigned_to": None,
                "subtask_id": None,
                "started_at": None,
                "completed_at": None,
                "result_summary": None,
                "rollback_count": 0,
            }
            if "meeting_template" in sdef:
                stage_entry["meeting_template"] = sdef["meeting_template"]
            # parallel_with: list of stage names that may run concurrently with this stage.
            # Scheduling logic is TODO — field is reserved for future parallel dispatch.
            if "parallel_with" in sdef:
                stage_entry["parallel_with"] = sdef["parallel_with"]
            stages.append(stage_entry)

        # Create chained subtasks for non-skipped stages.
        # Parallel stages (those with parallel_with) share the same predecessor
        # as their "anchor" stage, so they can start simultaneously.
        prev_subtask_id: str | None = None
        # Maps stage name → subtask_id, used to resolve parallel dependencies.
        stage_subtask_map: dict[str, str] = {}
        # Track the subtask_id that the *next serial* stage should depend on.
        # When a group of parallel stages all complete, the next serial stage
        # waits for all of them — but since the subtasks are chained by name
        # we only need to track which subtask represents the "end" of the
        # current serial position. For simplicity we track the last subtask of
        # the immediately preceding serial stage group.
        for stage in stages:
            if stage["status"] == STAGE_SKIPPED:
                continue

            # Determine dependency: parallel stages use the same predecessor
            # as their anchor stage (prev_subtask_id before the parallel group started).
            parallel_with: list[str] = stage.get("parallel_with", [])
            if parallel_with:
                # This stage can run alongside the stages listed in parallel_with.
                # Its subtask depends on the same predecessor as those stages
                # (i.e., the subtask before the parallel group).
                depends = [prev_subtask_id] if prev_subtask_id else []
            else:
                depends = [prev_subtask_id] if prev_subtask_id else []

            subtask = await self._repo.create_task(
                team_id=task.team_id,
                title=f"{task.title} — {stage['name']}",
                description=(
                    f"Pipeline 阶段: {stage['name']}\n"
                    f"建议Agent模板: {stage['agent_template']}\n"
                    f"父任务: {task.title}"
                ),
                parent_id=task_id,
                depth=1,
                order=stages.index(stage),
                depends_on=depends,
                project_id=task.project_id,
                config={"pipeline_stage": stage["name"], "pipeline_parent": task_id},
            )
            stage["subtask_id"] = subtask.id
            stage_subtask_map[stage["name"]] = subtask.id

            # Only advance prev_subtask_id for serial (non-parallel) stages,
            # so that the next stage waits for the last serial anchor.
            if not parallel_with:
                prev_subtask_id = subtask.id

        # If first non-skipped stage has no dependencies, it's ready
        # (depends_on is empty so it stays PENDING, not BLOCKED)

        # Save pipeline metadata to parent task config
        pipeline_meta: dict[str, Any] = {
            "type": pipeline_type,
            "current_stage_index": self._first_active_index(stages),
            "created_at": datetime.now().isoformat(),
            "stages": stages,
        }
        config = dict(task.config)
        config["pipeline"] = pipeline_meta
        await self._repo.update_task(task_id, config=config)

        logger.info(
            "Pipeline created: task=%s, type=%s, stages=%d",
            task_id,
            pipeline_type,
            len([s for s in stages if s["status"] != STAGE_SKIPPED]),
        )

        return {
            "success": True,
            "data": {
                "task_id": task_id,
                "pipeline_type": pipeline_type,
                "total_stages": len(stages),
                "active_stages": len([s for s in stages if s["status"] != STAGE_SKIPPED]),
                "stages": [
                    {
                        "name": s["name"],
                        "status": s["status"],
                        "agent_template": s["agent_template"],
                        "subtask_id": s["subtask_id"],
                    }
                    for s in stages
                ],
                "current_stage": self._current_stage_name(stages),
                "next_agent_template": self._current_agent_template(stages),
            },
            "message": (
                f"Pipeline '{pipeline_type}' 已创建，"
                f"共 {len([s for s in stages if s['status'] != STAGE_SKIPPED])} 个阶段。"
                f"当前阶段: {self._current_stage_name(stages)}，"
                f"建议使用 Agent 模板: {self._current_agent_template(stages)}"
            ),
        }

    # ----------------------------------------------------------------
    # Advance
    # ----------------------------------------------------------------

    async def advance_stage(self, task_id: str, result_summary: str = "") -> dict[str, Any]:
        """Mark current stage as completed and advance to the next.

        Args:
            task_id: Parent task ID (with pipeline).
            result_summary: Summary of what was accomplished in the completed stage.

        Returns:
            Next stage info + agent_template recommendation.
        """
        task = await self._repo.get_task(task_id)
        if task is None:
            return {"success": False, "error": f"任务 {task_id} 不存在"}

        pipeline = task.config.get("pipeline")
        if not pipeline:
            return {"success": False, "error": "任务没有 pipeline"}

        stages: list[dict[str, Any]] = pipeline["stages"]
        current_idx = pipeline.get("current_stage_index", 0)

        # Validate current stage
        if current_idx >= len(stages):
            return {"success": False, "error": "Pipeline 已完成，无可推进的阶段"}

        current = stages[current_idx]

        # If current_stage_index points to an already-completed stage (can happen
        # during parallel execution where a peer stage was completed first and
        # current_stage_index was not updated), find the next running stage.
        if current["status"] not in (STAGE_RUNNING, STAGE_PENDING):
            # Search for any running stage in the parallel group or elsewhere
            running_idx = None
            for i, s in enumerate(stages):
                if s["status"] == STAGE_RUNNING:
                    running_idx = i
                    break
            if running_idx is None:
                return {
                    "success": False,
                    "error": f"当前阶段 '{current['name']}' 状态为 {current['status']}，无法推进",
                }
            current_idx = running_idx
            current = stages[current_idx]

        # Mark current stage completed
        now = datetime.now().isoformat()
        current["status"] = STAGE_COMPLETED
        current["completed_at"] = now
        if result_summary:
            current["result_summary"] = result_summary

        # Mark current stage's subtask as completed
        if current.get("subtask_id"):
            try:
                await self._repo.update_task(
                    current["subtask_id"],
                    status=TaskStatus.COMPLETED.value,
                    completed_at=datetime.now(),
                )
            except Exception:
                logger.warning("Failed to update subtask %s to completed", current["subtask_id"])

        # --- Parallel completion gate ---
        # If the just-completed stage is part of a parallel group, check whether
        # all members of that group have also completed before moving forward.
        # A "parallel group" is defined as: the current stage + any stages whose
        # parallel_with list includes the current stage name.
        current_name = current["name"]
        parallel_peers = self._get_parallel_group(stages, current_name)
        # parallel_peers includes current stage itself.
        pending_peers = [
            s for s in parallel_peers
            if s["name"] != current_name and s["status"] not in (STAGE_COMPLETED, STAGE_SKIPPED, STAGE_FAILED)
        ]
        if pending_peers:
            # Other members of the parallel group are still running — hold position.
            config = dict(task.config)
            config["pipeline"] = pipeline
            await self._repo.update_task(task_id, config=config)

            logger.info(
                "Pipeline parallel hold: task=%s, completed=%s, waiting for=%s",
                task_id,
                current_name,
                [s["name"] for s in pending_peers],
            )
            return {
                "success": True,
                "data": {
                    "task_id": task_id,
                    "completed_stage": current_name,
                    "parallel_waiting": [s["name"] for s in pending_peers],
                    "current_stage": current_name,  # still in the parallel group
                    "parallel_group": [s["name"] for s in parallel_peers],
                },
                "message": (
                    f"阶段 '{current_name}' 已完成。"
                    f"等待并行阶段完成: {[s['name'] for s in pending_peers]}"
                ),
            }

        # All parallel peers are done (or there are none) — find next serial stage.
        # Advance index past the entire parallel group.
        # Find the highest index occupied by the parallel group, then scan forward.
        group_indices = [i for i, s in enumerate(stages) if any(ps["name"] == s["name"] for ps in parallel_peers)]
        after_group = max(group_indices) + 1 if group_indices else current_idx + 1

        next_idx = after_group
        while next_idx < len(stages) and stages[next_idx]["status"] == STAGE_SKIPPED:
            next_idx += 1

        if next_idx >= len(stages):
            # Pipeline complete — all stages done or skipped
            pipeline["current_stage_index"] = len(stages)
            config = dict(task.config)
            config["pipeline"] = pipeline
            await self._repo.update_task(task_id, config=config)

            # Auto-mark the parent task as completed when all stages are done/skipped.
            try:
                await self._repo.update_task(
                    task_id,
                    status=TaskStatus.COMPLETED.value,
                    completed_at=datetime.now(),
                )
                logger.info("Pipeline completed, parent task auto-marked completed: task=%s", task_id)
            except Exception:
                logger.warning("Failed to auto-complete parent task %s", task_id)

            # Check if the last completed stage was deploy — suggest git operations.
            completed_stages = [s for s in stages if s["status"] == STAGE_COMPLETED]
            last_completed = completed_stages[-1]["name"] if completed_stages else None
            git_suggestion = None
            if last_completed == "deploy":
                git_suggestion = (
                    "Deploy 阶段已完成，建议执行: "
                    "1) git_auto_commit 提交本次变更 "
                    "2) git_create_pr 创建 Pull Request"
                )

            result_data: dict[str, Any] = {
                "task_id": task_id,
                "pipeline_completed": True,
                "parent_task_completed": True,
                "stages_summary": self._stages_summary(stages),
            }
            if git_suggestion:
                result_data["_suggestion"] = git_suggestion

            return {
                "success": True,
                "data": result_data,
                "message": "Pipeline 所有阶段已完成！父任务已自动标记为 completed。"
                + (f" {git_suggestion}" if git_suggestion else ""),
            }

        # Advance to next stage — check if it heads a parallel group.
        next_stage = stages[next_idx]
        # Collect all stages that should start together with next_stage.
        # A stage belongs to the next parallel group if it lists next_stage["name"]
        # in its parallel_with, or if next_stage lists it in parallel_with.
        next_parallel_group = self._get_parallel_group(stages, next_stage["name"])
        unlocked_stages = []
        for stage in next_parallel_group:
            if stage["status"] == STAGE_PENDING:
                stage["status"] = STAGE_RUNNING
                stage["started_at"] = now
                unlocked_stages.append(stage)
                if stage.get("subtask_id"):
                    try:
                        await self._repo.update_task(
                            stage["subtask_id"],
                            status=TaskStatus.PENDING.value,
                        )
                    except Exception:
                        logger.warning("Failed to unblock subtask %s", stage["subtask_id"])

        # Update current_stage_index to point at the first stage of the new group.
        pipeline["current_stage_index"] = next_idx

        config = dict(task.config)
        config["pipeline"] = pipeline
        await self._repo.update_task(task_id, config=config)

        if len(unlocked_stages) > 1:
            logger.info(
                "Pipeline parallel advance: task=%s, %s → parallel(%s)",
                task_id,
                current_name,
                [s["name"] for s in unlocked_stages],
            )
            return {
                "success": True,
                "data": {
                    "task_id": task_id,
                    "completed_stage": current_name,
                    "parallel_stages_started": [s["name"] for s in unlocked_stages],
                    "current_stage": next_stage["name"],
                    "agent_templates": {s["name"]: s["agent_template"] for s in unlocked_stages},
                    "subtask_ids": {s["name"]: s.get("subtask_id") for s in unlocked_stages},
                    "remaining_stages": len(stages) - next_idx - len(unlocked_stages),
                    "progress": (
                        f"{len([s for s in stages if s['status'] == STAGE_COMPLETED])}"
                        f"/{len([s for s in stages if s['status'] != STAGE_SKIPPED])}"
                    ),
                },
                "message": (
                    f"阶段 '{current_name}' 已完成。"
                    f"并行启动阶段: {[s['name'] for s in unlocked_stages]}，"
                    f"共 {len(unlocked_stages)} 个并行阶段同时运行。"
                ),
            }

        logger.info(
            "Pipeline advanced: task=%s, %s → %s",
            task_id,
            current_name,
            next_stage["name"],
        )

        return {
            "success": True,
            "data": {
                "task_id": task_id,
                "completed_stage": current_name,
                "current_stage": next_stage["name"],
                "agent_template": next_stage["agent_template"],
                "subtask_id": next_stage["subtask_id"],
                "remaining_stages": len(stages) - next_idx - 1,
                "progress": f"{next_idx}/{len([s for s in stages if s['status'] != STAGE_SKIPPED])}",
            },
            "message": (
                f"阶段 '{current_name}' 已完成。"
                f"当前进入: '{next_stage['name']}'，"
                f"建议使用 Agent 模板: {next_stage['agent_template']}"
            ),
        }

    # ----------------------------------------------------------------
    # Fail / Rollback
    # ----------------------------------------------------------------

    async def fail_stage(self, task_id: str, reason: str = "") -> dict[str, Any]:
        """Mark current stage as failed, triggering rollback if applicable.

        Review/Test failures rollback to the corresponding implementation stage.
        Other failures mark stage as failed and require Leader intervention.

        Args:
            task_id: Parent task ID.
            reason: Failure reason.

        Returns:
            Rollback info or escalation notice.
        """
        task = await self._repo.get_task(task_id)
        if task is None:
            return {"success": False, "error": f"任务 {task_id} 不存在"}

        pipeline = task.config.get("pipeline")
        if not pipeline:
            return {"success": False, "error": "任务没有 pipeline"}

        stages: list[dict[str, Any]] = pipeline["stages"]
        current_idx = pipeline.get("current_stage_index", 0)

        if current_idx >= len(stages):
            return {"success": False, "error": "Pipeline 已完成，无可标记失败的阶段"}

        current = stages[current_idx]
        if current["status"] != STAGE_RUNNING:
            return {
                "success": False,
                "error": f"当前阶段 '{current['name']}' 状态为 {current['status']}，"
                f"只有 running 状态可以标记失败",
            }

        pipeline_type = pipeline["type"]
        stage_name = current["name"]
        rollback_map = ROLLBACK_MAP.get(pipeline_type, {})
        rollback_target = rollback_map.get(stage_name)

        if rollback_target and current["rollback_count"] < MAX_ROLLBACK_COUNT:
            # Rollback: reset current stage and target stage
            current["status"] = STAGE_PENDING
            current["started_at"] = None
            current["rollback_count"] += 1

            # Find rollback target stage index
            target_idx = None
            for i, s in enumerate(stages):
                if s["name"] == rollback_target:
                    target_idx = i
                    break

            if target_idx is not None:
                target_stage = stages[target_idx]
                target_stage["status"] = STAGE_RUNNING
                target_stage["started_at"] = datetime.now().isoformat()
                target_stage["completed_at"] = None
                pipeline["current_stage_index"] = target_idx

                # Reset target subtask to pending
                if target_stage.get("subtask_id"):
                    try:
                        await self._repo.update_task(
                            target_stage["subtask_id"],
                            status=TaskStatus.PENDING.value,
                            assigned_to=None,
                        )
                    except Exception:
                        pass

                config = dict(task.config)
                config["pipeline"] = pipeline
                await self._repo.update_task(task_id, config=config)

                logger.info(
                    "Pipeline rollback: task=%s, %s failed → back to %s (count=%d)",
                    task_id,
                    stage_name,
                    rollback_target,
                    current["rollback_count"],
                )

                return {
                    "success": True,
                    "data": {
                        "task_id": task_id,
                        "failed_stage": stage_name,
                        "reason": reason,
                        "action": "rollback",
                        "rollback_to": rollback_target,
                        "rollback_count": current["rollback_count"],
                        "max_rollbacks": MAX_ROLLBACK_COUNT,
                        "agent_template": target_stage["agent_template"],
                    },
                    "message": (
                        f"阶段 '{stage_name}' 失败，已回退到 '{rollback_target}'。"
                        f"回退次数: {current['rollback_count']}/{MAX_ROLLBACK_COUNT}。"
                        f"原因: {reason or '未指定'}"
                    ),
                }

        # No rollback target or max rollbacks exceeded — escalate
        current["status"] = STAGE_FAILED
        current["completed_at"] = datetime.now().isoformat()
        current["result_summary"] = f"失败: {reason}" if reason else "失败"

        config = dict(task.config)
        config["pipeline"] = pipeline
        await self._repo.update_task(task_id, config=config)

        exceeded = rollback_target and current["rollback_count"] >= MAX_ROLLBACK_COUNT
        logger.warning(
            "Pipeline stage failed (escalation): task=%s, stage=%s, reason=%s",
            task_id,
            stage_name,
            reason,
        )

        return {
            "success": True,
            "data": {
                "task_id": task_id,
                "failed_stage": stage_name,
                "reason": reason,
                "action": "escalate",
                "rollback_exceeded": exceeded,
            },
            "message": (
                f"阶段 '{stage_name}' 失败，需要 Leader 介入。"
                + (f"已达到最大回退次数 ({MAX_ROLLBACK_COUNT})。" if exceeded else "")
                + f"原因: {reason or '未指定'}"
            ),
        }

    # ----------------------------------------------------------------
    # Skip
    # ----------------------------------------------------------------

    async def skip_stage(self, task_id: str, stage_name: str) -> dict[str, Any]:
        """Skip a specific stage (Leader manual action).

        Args:
            task_id: Parent task ID.
            stage_name: Name of the stage to skip.

        Returns:
            Updated pipeline status.
        """
        task = await self._repo.get_task(task_id)
        if task is None:
            return {"success": False, "error": f"任务 {task_id} 不存在"}

        pipeline = task.config.get("pipeline")
        if not pipeline:
            return {"success": False, "error": "任务没有 pipeline"}

        stages: list[dict[str, Any]] = pipeline["stages"]
        target = None
        target_idx = None
        for i, s in enumerate(stages):
            if s["name"] == stage_name:
                target = s
                target_idx = i
                break

        if target is None:
            return {"success": False, "error": f"阶段 '{stage_name}' 不存在"}

        if target["status"] in (STAGE_COMPLETED, STAGE_SKIPPED):
            return {"success": False, "error": f"阶段 '{stage_name}' 已经是 {target['status']} 状态"}

        target["status"] = STAGE_SKIPPED

        # If skipping the current stage, advance index
        current_idx = pipeline.get("current_stage_index", 0)
        if target_idx == current_idx:
            next_idx = current_idx + 1
            while next_idx < len(stages) and stages[next_idx]["status"] == STAGE_SKIPPED:
                next_idx += 1
            pipeline["current_stage_index"] = next_idx

        # Mark subtask as completed (skipped)
        if target.get("subtask_id"):
            try:
                await self._repo.update_task(
                    target["subtask_id"],
                    status=TaskStatus.COMPLETED.value,
                    result="skipped",
                    completed_at=datetime.now(),
                )
            except Exception:
                pass

        config = dict(task.config)
        config["pipeline"] = pipeline
        await self._repo.update_task(task_id, config=config)

        logger.info("Pipeline stage skipped: task=%s, stage=%s", task_id, stage_name)

        return {
            "success": True,
            "data": {
                "task_id": task_id,
                "skipped_stage": stage_name,
                "current_stage": self._current_stage_name(stages, pipeline.get("current_stage_index", 0)),
            },
            "message": f"阶段 '{stage_name}' 已跳过。",
        }

    # ----------------------------------------------------------------
    # Status
    # ----------------------------------------------------------------

    async def get_pipeline_status(self, task_id: str) -> dict[str, Any]:
        """Get pipeline progress overview for a task.

        Args:
            task_id: Parent task ID.

        Returns:
            Pipeline status including all stages, progress percentage, and current stage.
        """
        task = await self._repo.get_task(task_id)
        if task is None:
            return {"success": False, "error": f"任务 {task_id} 不存在"}

        pipeline = task.config.get("pipeline")
        if not pipeline:
            return {"success": False, "error": "任务没有 pipeline"}

        stages: list[dict[str, Any]] = pipeline["stages"]
        active_stages = [s for s in stages if s["status"] != STAGE_SKIPPED]
        completed = [s for s in active_stages if s["status"] == STAGE_COMPLETED]
        failed = [s for s in active_stages if s["status"] == STAGE_FAILED]
        running = [s for s in active_stages if s["status"] == STAGE_RUNNING]

        total_active = len(active_stages)
        progress_pct = round(len(completed) / total_active * 100) if total_active > 0 else 0

        pipeline_completed = len(completed) == total_active and not failed and not running

        current_idx = pipeline.get("current_stage_index", 0)
        current_stage = self._current_stage_name(stages, current_idx)
        current_template = self._current_agent_template(stages, current_idx)

        return {
            "success": True,
            "data": {
                "task_id": task_id,
                "task_title": task.title,
                "pipeline_type": pipeline["type"],
                "pipeline_completed": pipeline_completed,
                "progress": f"{len(completed)}/{total_active}",
                "progress_pct": progress_pct,
                "current_stage": current_stage,
                "current_agent_template": current_template,
                "stages": [
                    {
                        "name": s["name"],
                        "status": s["status"],
                        "agent_template": s["agent_template"],
                        "assigned_to": s.get("assigned_to"),
                        "subtask_id": s.get("subtask_id"),
                        "started_at": s.get("started_at"),
                        "completed_at": s.get("completed_at"),
                        "result_summary": s.get("result_summary"),
                        "rollback_count": s.get("rollback_count", 0),
                        "parallel_with": s.get("parallel_with", []),
                    }
                    for s in stages
                ],
                "parallel_running": [s["name"] for s in running if s.get("parallel_with")],
                "stats": {
                    "total": len(stages),
                    "active": total_active,
                    "completed": len(completed),
                    "running": len(running),
                    "failed": len(failed),
                    "skipped": len(stages) - total_active,
                },
                "created_at": pipeline.get("created_at"),
            },
        }

    # ----------------------------------------------------------------
    # Helpers
    # ----------------------------------------------------------------

    @staticmethod
    def _get_parallel_group(stages: list[dict[str, Any]], stage_name: str) -> list[dict[str, Any]]:
        """Return all stages that belong to the same parallel group as stage_name.

        A parallel group is defined as a set of stages that mutually reference
        each other via the parallel_with field.  The anchor stage (the one that
        does NOT list parallel_with itself but IS listed by others) is also
        included.

        If stage_name has no parallel peers, the returned list contains only
        the stage itself.
        """
        target = next((s for s in stages if s["name"] == stage_name), None)
        if target is None:
            return []

        # Collect names that are in the same parallel group.
        group_names: set[str] = {stage_name}
        # Stages listed in target's own parallel_with.
        for peer_name in target.get("parallel_with", []):
            group_names.add(peer_name)
        # Stages whose parallel_with includes stage_name.
        for s in stages:
            if stage_name in s.get("parallel_with", []):
                group_names.add(s["name"])
                # Also include transitive peers (their parallel_with members).
                for peer_name in s.get("parallel_with", []):
                    group_names.add(peer_name)

        return [s for s in stages if s["name"] in group_names]

    @staticmethod
    def _first_active_index(stages: list[dict[str, Any]]) -> int:
        """Find the index of the first non-skipped stage."""
        for i, s in enumerate(stages):
            if s["status"] != STAGE_SKIPPED:
                return i
        return 0

    @staticmethod
    def _current_stage_name(
        stages: list[dict[str, Any]], current_idx: int | None = None
    ) -> str | None:
        """Get the name of the current stage."""
        if current_idx is None:
            for s in stages:
                if s["status"] != STAGE_SKIPPED:
                    return s["name"]
            return None
        if current_idx < len(stages):
            return stages[current_idx]["name"]
        return None

    @staticmethod
    def _current_agent_template(
        stages: list[dict[str, Any]], current_idx: int | None = None
    ) -> str | None:
        """Get the agent template for the current stage."""
        if current_idx is None:
            for s in stages:
                if s["status"] != STAGE_SKIPPED:
                    return s["agent_template"]
            return None
        if current_idx < len(stages):
            return stages[current_idx]["agent_template"]
        return None

    @staticmethod
    def _stages_summary(stages: list[dict[str, Any]]) -> list[dict[str, str]]:
        """Generate a compact summary of all stages."""
        return [
            {"name": s["name"], "status": s["status"], "result": s.get("result_summary") or ""}
            for s in stages
        ]
