"""Failure alchemy — distill defense rules, training cases, and improvement proposals from failures."""

from __future__ import annotations

import logging
from datetime import datetime

from aiteam.storage.repository import StorageRepository

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

    async def process_failure(self, task_id: str, team_id: str) -> dict:
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

        await self._repo.create_memory(
            scope="team",
            scope_id=team_id,
            content=(
                f"失败分析: {task.title}\n\n"
                f"抗体: {antibody}\n\n"
                f"疫苗: {vaccine}\n\n"
                f"催化剂: {catalyst}"
            ),
            metadata={
                "type": "failure_alchemy",
                "task_id": task_id,
                "task_title": task.title,
                "antibody": antibody,
                "vaccine": vaccine,
                "catalyst": catalyst,
                "created_at": datetime.now().isoformat(),
            },
        )

        logger.info(
            "FailureAlchemist: 失败任务 '%s' 已提炼为学习产物", task.title
        )
        return {"antibody": antibody, "vaccine": vaccine, "catalyst": catalyst}

    def _generate_antibody(self, task) -> str:
        """Extract defense rule suggestions from a failure."""
        result = task.result or ""
        error_info = (
            task.config.get("error", "") if isinstance(task.config, dict) else ""
        )
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
