"""失败炼金术 — 从失败中提炼防御规则、培训案例和改进提案."""

from __future__ import annotations

import logging
from datetime import datetime

from aiteam.storage.repository import StorageRepository

logger = logging.getLogger(__name__)


class FailureAlchemist:
    """失败炼金术师 — 将失败任务转化为三种学习产物.

    产物：
    - 抗体 (antibody): 防御规则建议，防止同类失败重演
    - 疫苗 (vaccine): 结构化失败案例，供新Agent学习参考
    - 催化剂 (catalyst): 系统改进提案，推动流程优化
    """

    def __init__(self, repo: StorageRepository) -> None:
        self._repo = repo

    async def process_failure(self, task_id: str, team_id: str) -> dict:
        """处理一个失败的任务，提炼三种产物并保存到团队记忆.

        Args:
            task_id: 失败任务的 ID
            team_id: 所属团队的 ID

        Returns:
            包含 antibody、vaccine、catalyst 三种产物的字典；
            若任务不存在则返回 {"error": "task not found"}
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
        """从失败中提取防御规则建议."""
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
        """生成结构化失败案例供新 Agent 学习."""
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
        """生成系统改进提案."""
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
