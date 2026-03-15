"""AI Team OS — Watchdog检查器 + 后台巡检服务.

规则驱动的质量门，检查Agent健康、任务健康、系统健康。
WatchdogChecker: 由API端点按需触发，返回告警列表。
WatchdogRunner: asyncio.Task后台自动巡检，定期对所有active团队执行检查。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any

from aiteam.config.settings import WATCHDOG_CHECK_INTERVAL
from aiteam.storage.repository import StorageRepository
from aiteam.types import AgentStatus, TaskStatus, TeamStatus

if TYPE_CHECKING:
    from aiteam.api.event_bus import EventBus

logger = logging.getLogger(__name__)

# 阈值常量
AGENT_BUSY_TIMEOUT_MINUTES = 30
TASK_PENDING_TIMEOUT_MINUTES = 30


class WatchdogChecker:
    """Watchdog检查器 — 规则驱动的质量门."""

    def __init__(self, repo: StorageRepository) -> None:
        self._repo = repo

    async def run_all_checks(self, team_id: str) -> list[dict[str, Any]]:
        """运行所有检查项，返回告警列表."""
        alerts: list[dict[str, Any]] = []

        alerts.extend(await self.check_agent_health(team_id))
        alerts.extend(await self.check_task_health(team_id))
        alerts.extend(await self.check_system_health())

        return alerts

    async def check_agent_health(self, team_id: str) -> list[dict[str, Any]]:
        """检查Agent健康：BUSY超时(>30min)、频繁crash."""
        alerts: list[dict[str, Any]] = []
        now = datetime.now()
        agents = await self._repo.list_agents(team_id)

        for agent in agents:
            # 检查BUSY超时
            if agent.status == AgentStatus.BUSY:
                ref_time = agent.last_active_at or agent.created_at
                elapsed_minutes = (now - ref_time).total_seconds() / 60

                if elapsed_minutes > AGENT_BUSY_TIMEOUT_MINUTES:
                    alerts.append({
                        "severity": "warning",
                        "category": "agent",
                        "title": f"Agent BUSY超时: {agent.name}",
                        "description": (
                            f"Agent '{agent.name}' 已处于BUSY状态 "
                            f"{elapsed_minutes:.0f} 分钟（阈值 {AGENT_BUSY_TIMEOUT_MINUTES} 分钟）。"
                            f"上次活动: {ref_time.isoformat()}"
                        ),
                        "suggested_action": (
                            f"检查Agent '{agent.name}' 是否卡死，"
                            "考虑通过StateReaper重置或手动设为IDLE"
                        ),
                        "agent_id": agent.id,
                        "agent_name": agent.name,
                    })

        return alerts

    async def check_task_health(self, team_id: str) -> list[dict[str, Any]]:
        """检查任务健康：长时间PENDING(>30min)、BLOCKED但依赖已完成."""
        alerts: list[dict[str, Any]] = []
        now = datetime.now()
        all_tasks = await self._repo.list_tasks(team_id)

        # 建立task_id → task的索引
        task_map = {t.id: t for t in all_tasks}

        for task in all_tasks:
            # 检查长时间PENDING
            if task.status == TaskStatus.PENDING:
                elapsed_minutes = (now - task.created_at).total_seconds() / 60

                if elapsed_minutes > TASK_PENDING_TIMEOUT_MINUTES:
                    alerts.append({
                        "severity": "warning",
                        "category": "task",
                        "title": f"任务长时间PENDING: {task.title}",
                        "description": (
                            f"任务 '{task.title}' 已等待 {elapsed_minutes:.0f} 分钟"
                            f"（阈值 {TASK_PENDING_TIMEOUT_MINUTES} 分钟），"
                            f"优先级: {task.priority}"
                        ),
                        "suggested_action": (
                            "分配Agent执行此任务，或降低优先级"
                        ),
                        "task_id": task.id,
                    })

            # 检查BLOCKED但依赖已完成
            if task.status == TaskStatus.BLOCKED and task.depends_on:
                deps_all_done = True
                for dep_id in task.depends_on:
                    dep_task = task_map.get(dep_id)
                    if dep_task is None:
                        continue
                    if dep_task.status != TaskStatus.COMPLETED:
                        deps_all_done = False
                        break

                if deps_all_done:
                    alerts.append({
                        "severity": "warning",
                        "category": "task",
                        "title": f"任务可解除阻塞: {task.title}",
                        "description": (
                            f"任务 '{task.title}' 状态为BLOCKED，"
                            "但所有依赖任务已完成"
                        ),
                        "suggested_action": (
                            "将此任务状态从BLOCKED更新为PENDING"
                        ),
                        "task_id": task.id,
                    })

        return alerts

    async def check_system_health(self) -> list[dict[str, Any]]:
        """检查系统健康：数据库可达性."""
        alerts: list[dict[str, Any]] = []

        # 检查数据库连接
        try:
            await self._repo.list_teams()
        except Exception as e:
            alerts.append({
                "severity": "critical",
                "category": "system",
                "title": "数据库连接异常",
                "description": f"无法查询数据库: {e}",
                "suggested_action": "检查数据库配置和连接状态",
            })

        return alerts


class WatchdogRunner:
    """后台Watchdog巡检服务 — asyncio.Task模式.

    定期遍历所有active团队，运行WatchdogChecker的全部检查项，
    将告警写入EventBus。模式参考StateReaper。
    """

    def __init__(
        self,
        checker: WatchdogChecker,
        event_bus: EventBus,
    ) -> None:
        self._checker = checker
        self._event_bus = event_bus
        self._task: asyncio.Task | None = None
        self._running = False

    def start(self) -> None:
        """启动后台巡检循环."""
        if self._task is not None:
            logger.warning("WatchdogRunner已在运行，跳过重复启动")
            return
        self._running = True
        self._task = asyncio.create_task(self._run_loop(), name="watchdog-runner")
        logger.info("WatchdogRunner已启动，间隔=%ds", WATCHDOG_CHECK_INTERVAL)

    async def stop(self) -> None:
        """停止后台巡检循环."""
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
            logger.info("WatchdogRunner已停止")

    async def _run_loop(self) -> None:
        """巡检主循环 — 每WATCHDOG_CHECK_INTERVAL秒执行一次."""
        while self._running:
            try:
                await asyncio.wait_for(self._run_cycle(), timeout=30.0)
            except TimeoutError:
                logger.warning("Watchdog巡检周期超时（30s），跳过本轮")
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Watchdog巡检周期异常")

            try:
                await asyncio.sleep(WATCHDOG_CHECK_INTERVAL)
            except asyncio.CancelledError:
                break

    async def _run_cycle(self) -> None:
        """单次巡检 — 遍历所有active团队运行检查."""
        repo = self._checker._repo
        teams = await repo.list_teams()
        active_teams = [t for t in teams if t.status == TeamStatus.ACTIVE]
        alert_count = 0

        for team in active_teams:
            alerts = await self._checker.run_all_checks(team.id)
            for alert in alerts:
                await self._event_bus.emit(
                    "watchdog.alert",
                    f"team:{team.id}",
                    alert,
                )
                alert_count += 1

        if alert_count > 0:
            logger.warning("Watchdog巡检发现 %d 个告警", alert_count)
        else:
            logger.debug("Watchdog巡检完成，无告警")
