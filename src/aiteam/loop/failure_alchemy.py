"""Failure alchemy — distill defense rules, training cases, and improvement proposals from failures."""

from __future__ import annotations

import logging
from typing import Any

from aiteam.clock import utc_now
from aiteam.storage.repository import StorageRepository
from aiteam.types import EventType

logger = logging.getLogger(__name__)


class FailureAlchemist:
    """Failure alchemist — transform failed tasks into three learning artifacts.

    Artifacts:
    - Antibody: Defense rule suggestions to prevent similar failures
    - Vaccine: Structured failure cases for new agent onboarding
    - Catalyst: System improvement proposals to drive process optimization
    """

    def __init__(self, repo: StorageRepository) -> None:
        self._repo = repo

    async def process_failure(self, task_id: str, team_id: str, template_name: str = "") -> dict:
        """Process a failed task and distill three learning artifacts, saving them to team memory.

        Args:
            task_id: ID of the failed task.
            team_id: ID of the owning team.

        Returns:
            Dict containing antibody, vaccine, and catalyst artifacts;
            returns {"error": "task not found"} if the task does not exist.
        """
        task = await self._repo.get_task(task_id)
        if not task:
            logger.warning("FailureAlchemist: task %s not found", task_id)
            return {"error": "task not found"}

        antibody = self._generate_antibody(task)
        vaccine = self._generate_vaccine(task)
        catalyst = self._generate_catalyst(task)

        failure_meta: dict = {
            "type": "failure_alchemy",
            "task_id": task_id,
            "task_title": task.title,
            "antibody": antibody,
            "vaccine": vaccine,
            "catalyst": catalyst,
            "created_at": utc_now().isoformat(),
        }
        # Associate with agent template if provided — enables prompt effectiveness tracking
        if template_name:
            failure_meta["template_name"] = template_name

        # 归属层级：project 而非 team（2026-07-27 裁定）。团队在 CC v2.1.219 下是
        # 会话级/单次 workflow 级的短命容器——一支队关掉，挂在它名下的失败教训就
        # 再没人读得到；实测本机已有 125 条这样的孤儿。项目才是教训真正该沉淀的
        # 层级（同一项目的下一支队、下一次 workflow 都该读得到）。
        # 历史 125 条**不迁移**（留作 team 层归档，避免改写历史行的归属）。
        # 拿不到项目归属时（无 team 或队未绑定项目）退回 team 层，绝不丢记录。
        scope, scope_id = "team", team_id
        try:
            team = await self._repo.get_team(team_id)
            project_id = getattr(team, "project_id", None) if team else None
            if project_id:
                scope, scope_id = "project", project_id
                failure_meta["team_id"] = team_id
        except Exception:
            logger.debug("FailureAlchemist: 项目归属解析失败，记忆退回 team 层")

        await self._repo.create_memory(
            scope=scope,
            scope_id=scope_id,
            content=(
                f"失败分析: {task.title}\n\n"
                f"抗体: {antibody}\n\n"
                f"疫苗: {vaccine}\n\n"
                f"催化剂: {catalyst}"
            ),
            metadata=failure_meta,
        )

        logger.info("FailureAlchemist: 失败任务 '%s' 已提炼为学习产物", task.title)

        # Emitted here rather than in the route handler so direct callers are
        # covered too.  Best-effort: a bookkeeping miss must never sink the
        # analysis the caller actually asked for.
        try:
            await self._repo.create_event(
                event_type=EventType.TASK_FAILURE_ANALYZED.value,
                source="failure_alchemist",
                data={
                    "task_id": task_id,
                    "task_title": task.title,
                    "team_id": team_id,
                    "scope": scope,
                    "scope_id": scope_id,
                    "artifacts": ["antibody", "vaccine", "catalyst"],
                },
                entity_id=task_id,
                entity_type="task",
            )
        except Exception:
            logger.debug("FailureAlchemist: 失败炼金事件落库失败（不影响分析结果）")

        return {"antibody": antibody, "vaccine": vaccine, "catalyst": catalyst}

    def _generate_antibody(self, task) -> str:
        """Extract defense rule suggestions from a failure."""
        result = task.result or ""
        error_info = task.config.get("error", "") if isinstance(task.config, dict) else ""
        failure_context = result or error_info or "未记录失败原因"

        return (
            f"防御规则建议：任务「{task.title}」失败。\n"
            f"失败原因：{failure_context[:200]}\n"
            f"建议：在类似任务开始前检查相关前置条件"
        )

    def _generate_vaccine(self, task) -> str:
        """Generate a structured failure case for new agent onboarding."""
        description = task.description[:150] if task.description else "无"
        result_summary = (task.result or "未记录")[:200]
        prevention = (
            task.config.get("error", "检查前置条件")
            if isinstance(task.config, dict)
            else "检查前置条件"
        )

        return (
            f"## 失败案例：{task.title}\n"
            f"- 任务描述：{description}\n"
            f"- 分配给：{task.assigned_to or '未分配'}\n"
            f"- 失败结果：{result_summary}\n"
            f"- 教训：执行此类任务前应先确认环境和依赖就绪\n"
            f"- 预防措施：{prevention}"
        )

    def _generate_catalyst(self, task) -> str:
        """Generate a system improvement proposal."""
        tags = task.tags if task.tags else []
        domain = ", ".join(tags) if tags else "通用"

        return (
            f"改进提案：「{task.title}」失败分析\n"
            f"- 涉及领域：{domain}\n"
            f"- 建议：\n"
            f"  1) 检查此类任务的前置条件清单\n"
            f"  2) 增加相关自动化测试\n"
            f"  3) 考虑添加Watchdog检测规则"
        )

    async def diagnose_failure(self, task_id: str) -> dict[str, Any]:
        """Auto-diagnose why a task failed by analyzing execution trace and memos.

        Reads the task's memo history to identify the failure point, compares with
        similar successful tasks in the same team, and generates actionable fix suggestions.

        Args:
            task_id: ID of the failed (or any) task to diagnose.

        Returns:
            Diagnosis report with root_cause, failed_at, similar_successes count,
            suggested_fixes, and rollback_recommendation.
        """
        task = await self._repo.get_task(task_id)
        if not task:
            logger.warning("FailureAlchemist.diagnose_failure: task %s not found", task_id)
            return {"error": "task not found"}

        # Extract memo trace
        memos: list[dict] = task.config.get("memo", []) if isinstance(task.config, dict) else []

        # Identify failure point — last memo with type "issue" or containing error keywords
        failed_at = "未记录"
        error_memo: dict | None = None
        error_keywords = ("失败", "error", "exception", "failed", "blocked", "超时", "timeout")
        for memo in reversed(memos):
            content_lower = (memo.get("content", "") or "").lower()
            if memo.get("type") == "issue" or any(kw in content_lower for kw in error_keywords):
                error_memo = memo
                failed_at = memo.get("timestamp", "未知时间")
                break

        # Determine root cause from task result / error config / memos
        result_text = task.result or ""
        config_error = task.config.get("error", "") if isinstance(task.config, dict) else ""
        memo_error = (error_memo.get("content", "") if error_memo else "")
        root_cause_raw = result_text or config_error or memo_error or "未记录失败原因"
        root_cause = root_cause_raw[:300]

        # Count similar successful tasks in the same team (keyword overlap >= 2 in title)
        similar_successes = 0
        if task.team_id:
            from aiteam.types import TaskStatus

            all_tasks = await self._repo.list_tasks(task.team_id, status=TaskStatus.COMPLETED)
            task_words = set((task.title or "").lower().split())
            for t in all_tasks:
                if t.id == task_id:
                    continue
                other_words = set((t.title or "").lower().split())
                if len(task_words & other_words) >= 2:
                    similar_successes += 1

        # Generate fix suggestions based on failure context
        suggested_fixes = _generate_fix_suggestions(task, root_cause, memos)

        # Rollback recommendation
        rollback_recommendation = _build_rollback_recommendation(task)

        logger.info(
            "FailureAlchemist.diagnose_failure: 任务 '%s' 诊断完成，根因: %s",
            task.title,
            root_cause[:80],
        )

        diagnosis = {
            "task_id": task_id,
            "task_title": task.title,
            "root_cause": root_cause,
            "failed_at": failed_at,
            "similar_successes": similar_successes,
            "suggested_fixes": suggested_fixes,
            "rollback_recommendation": rollback_recommendation,
        }

        # Same rationale as process_failure: the diagnosis has to leave a trace
        # in the event stream, otherwise "was this failure ever looked at?" is
        # unanswerable after the fact.
        try:
            await self._repo.create_event(
                event_type=EventType.TASK_FAILURE_DIAGNOSED.value,
                source="failure_alchemist",
                data={
                    "task_id": task_id,
                    "task_title": task.title,
                    "team_id": task.team_id,
                    "root_cause": root_cause,
                    "failed_at": failed_at,
                    "similar_successes": similar_successes,
                    "suggested_fix_count": len(suggested_fixes),
                },
                entity_id=task_id,
                entity_type="task",
            )
        except Exception:
            logger.debug("FailureAlchemist: 失败诊断事件落库失败（不影响诊断结果）")

        return diagnosis


def _generate_fix_suggestions(task: Any, root_cause: str, memos: list[dict]) -> list[str]:
    """Generate actionable fix suggestions based on failure context."""
    fixes: list[str] = []
    root_lower = root_cause.lower()

    # Dependency / blocked failures
    if "blocked" in root_lower or "依赖" in root_lower or "depend" in root_lower:
        fixes.append("检查并完成所有前置依赖任务后再重试")

    # Timeout failures
    if "timeout" in root_lower or "超时" in root_lower or "timed out" in root_lower:
        fixes.append("增加超时阈值或将任务拆分为更小的子任务")

    # Import / module errors
    if "importerror" in root_lower or "modulenot" in root_lower or "import" in root_lower:
        fixes.append("检查依赖包是否已安装（pip install -r requirements.txt）")

    # Permission / auth failures
    if any(k in root_lower for k in ("permission", "unauthorized", "forbidden", "403", "401")):
        fixes.append("检查认证凭证和权限配置是否正确")

    # Network / connection failures
    if any(k in root_lower for k in ("connection", "network", "unreachable", "refused")):
        fixes.append("检查网络连接和目标服务是否可用")

    # Generic fallback suggestions always appended
    if task.assigned_to:
        fixes.append(f"联系执行agent「{task.assigned_to}」确认具体错误详情")
    if memos:
        issue_memos = [m for m in memos if m.get("type") == "issue"]
        if issue_memos:
            fixes.append(f"参考任务memo中 {len(issue_memos)} 条issue记录排查根因")

    if not fixes:
        fixes.append("查看任务执行日志并确认环境前置条件")
        fixes.append("尝试降低任务复杂度后重新执行")

    return fixes[:5]  # Return at most 5 suggestions


def _build_rollback_recommendation(task: Any) -> str:
    """Build a rollback recommendation based on task state."""
    from aiteam.types import TaskStatus

    status_val = task.status.value if hasattr(task.status, "value") else str(task.status)

    if status_val == TaskStatus.FAILED.value if hasattr(TaskStatus, "FAILED") else "failed":
        return "将任务状态重置为 pending，修复根因后重新分配执行"

    if status_val == TaskStatus.BLOCKED.value if hasattr(TaskStatus, "BLOCKED") else "blocked":
        return "解决依赖阻塞后，任务将自动解锁恢复执行"

    # Running but suspected failed
    return "可将任务状态更新为 pending 并清空 result 字段后重新执行"
