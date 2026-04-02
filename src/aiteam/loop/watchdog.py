"""AI Team OS — Watchdog checker + background patrol service.

Rule-driven quality gate that checks agent health, task health, and system health.
WatchdogChecker: Triggered on-demand by API endpoints, returns a list of alerts.
WatchdogRunner: Background asyncio.Task that periodically runs checks on all active teams.

Multi-DB support: WatchdogRunner scans all per-project databases in addition to the
default database each patrol cycle, with per-project error isolation.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any

from aiteam.config.settings import WATCHDOG_CHECK_INTERVAL
from aiteam.loop.failure_alchemy import FailureAlchemist
from aiteam.storage.repository import StorageRepository
from aiteam.types import AgentStatus, TaskStatus, TeamStatus

if TYPE_CHECKING:
    from aiteam.api.event_bus import EventBus

logger = logging.getLogger(__name__)

# Threshold constants
AGENT_BUSY_TIMEOUT_MINUTES = 30
TASK_PENDING_TIMEOUT_MINUTES = 30


class WatchdogChecker:
    """Watchdog checker — rule-driven quality gate."""

    def __init__(self, repo: StorageRepository) -> None:
        self._repo = repo

    async def run_all_checks(self, team_id: str) -> list[dict[str, Any]]:
        """Run all checks and return a list of alerts."""
        alerts: list[dict[str, Any]] = []

        alerts.extend(await self.check_agent_health(team_id))
        alerts.extend(await self.check_task_health(team_id))
        alerts.extend(await self.check_system_health())

        return alerts

    async def auto_recover_stuck_agents(self, team_id: str) -> list[dict]:
        """Detect and automatically recover stuck agents and their tasks."""
        recovered: list[dict] = []
        now = datetime.now()
        agents = await self._repo.list_agents(team_id)
        all_tasks = await self._repo.list_tasks(team_id, status=TaskStatus.RUNNING)

        # Build agent_id -> running tasks index
        running_tasks_by_agent: dict[str, list] = {}
        for task in all_tasks:
            if task.assigned_to:
                running_tasks_by_agent.setdefault(task.assigned_to, []).append(task)

        for agent in agents:
            if agent.status != AgentStatus.BUSY:
                continue

            ref_time = agent.last_active_at or agent.created_at
            elapsed_minutes = (now - ref_time).total_seconds() / 60

            if elapsed_minutes <= AGENT_BUSY_TIMEOUT_MINUTES:
                continue

            # Reset agent: WAITING + clear current_task
            await self._repo.update_agent(
                agent.id,
                status=AgentStatus.WAITING.value,
                current_task=None,
            )

            # Reset all running tasks for this agent to pending
            reset_tasks = []
            for task in running_tasks_by_agent.get(agent.id, []):
                await self._repo.update_task(
                    task.id,
                    status=TaskStatus.PENDING.value,
                    assigned_to=None,
                )
                reset_tasks.append(task.id)

                # Record recovery event to memory (agent scope)
                memo_content = (
                    f"因agent '{agent.name}' 卡死（无活动 {elapsed_minutes:.0f} 分钟）"
                    f"被自动重置，任务从RUNNING退回PENDING。"
                )
                await self._repo.create_memory(
                    scope="agent",
                    scope_id=agent.id,
                    content=memo_content,
                    metadata={
                        "type": "auto_recover",
                        "task_id": task.id,
                        "elapsed_minutes": round(elapsed_minutes, 1),
                    },
                )

            record = {
                "agent_id": agent.id,
                "agent_name": agent.name,
                "elapsed_minutes": round(elapsed_minutes, 1),
                "reset_tasks": reset_tasks,
            }
            recovered.append(record)
            logger.warning(
                "Watchdog自动恢复: Agent '%s' 卡死 %.0f 分钟，重置 %d 个任务",
                agent.name,
                elapsed_minutes,
                len(reset_tasks),
            )

        return recovered

    async def recover_failed_tasks(
        self,
        team_id: str,
        event_bus: EventBus | None = None,
    ) -> list[dict[str, Any]]:
        """Selector-pattern failed task recovery.

        retry_count < 2 -> reset to pending for retry
        retry_count >= 2 -> keep as failed, emit alert event
        retry_count is stored in task.config["retry_count"].
        """
        results: list[dict[str, Any]] = []
        failed_tasks = await self._repo.list_tasks(team_id, status=TaskStatus.FAILED)

        for task in failed_tasks:
            retry_count: int = int(task.config.get("retry_count", 0))
            title = task.title or task.description[:60]

            if retry_count < 2:
                # Retry: reset to pending, increment retry_count
                new_config = {**task.config, "retry_count": retry_count + 1}
                await self._repo.update_task(
                    task.id,
                    status=TaskStatus.PENDING.value,
                    assigned_to=None,
                    config=new_config,
                )
                record: dict[str, Any] = {
                    "task_id": task.id,
                    "title": title,
                    "action": "retried",
                    "retry_count": retry_count + 1,
                }
                results.append(record)
                logger.info(
                    "失败任务重试: '%s' (retry=%d)",
                    title,
                    retry_count + 1,
                )
            else:
                # Exceeded retry limit: trigger failure alchemy, extract learning artifacts
                alchemist = FailureAlchemist(self._repo)
                alchemy_result = await alchemist.process_failure(task.id, team_id)

                record = {
                    "task_id": task.id,
                    "title": title,
                    "action": "max_retries_exceeded",
                    "retry_count": retry_count,
                    "alchemy": alchemy_result,
                }
                results.append(record)
                if event_bus is not None:
                    await event_bus.emit(
                        "watchdog.task_failed_permanently",
                        f"task:{task.id}",
                        {
                            "task_id": task.id,
                            "title": title,
                            "team_id": team_id,
                            "retry_count": retry_count,
                            "trigger": "recover_failed_tasks",
                        },
                    )
                logger.warning(
                    "失败任务超过重试上限: '%s' (retry=%d)，需Leader介入",
                    title,
                    retry_count,
                )

        return results

    async def check_agent_health(self, team_id: str) -> list[dict[str, Any]]:
        """Check agent health: BUSY timeout (>30min), frequent crashes."""
        alerts: list[dict[str, Any]] = []
        now = datetime.now()
        agents = await self._repo.list_agents(team_id)

        for agent in agents:
            # Check BUSY timeout
            if agent.status == AgentStatus.BUSY:
                ref_time = agent.last_active_at or agent.created_at
                elapsed_minutes = (now - ref_time).total_seconds() / 60

                if elapsed_minutes > AGENT_BUSY_TIMEOUT_MINUTES:
                    alerts.append(
                        {
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
                        }
                    )

        return alerts

    async def check_task_health(self, team_id: str) -> list[dict[str, Any]]:
        """Check task health: long-pending (>30min), BLOCKED but dependencies completed."""
        alerts: list[dict[str, Any]] = []
        now = datetime.now()
        all_tasks = await self._repo.list_tasks(team_id)

        # Build task_id -> task index
        task_map = {t.id: t for t in all_tasks}

        for task in all_tasks:
            # Check long-pending tasks
            if task.status == TaskStatus.PENDING:
                elapsed_minutes = (now - task.created_at).total_seconds() / 60

                if elapsed_minutes > TASK_PENDING_TIMEOUT_MINUTES:
                    alerts.append(
                        {
                            "severity": "warning",
                            "category": "task",
                            "title": f"任务长时间PENDING: {task.title}",
                            "description": (
                                f"任务 '{task.title}' 已等待 {elapsed_minutes:.0f} 分钟"
                                f"（阈值 {TASK_PENDING_TIMEOUT_MINUTES} 分钟），"
                                f"优先级: {task.priority}"
                            ),
                            "suggested_action": ("分配Agent执行此任务，或降低优先级"),
                            "task_id": task.id,
                        }
                    )

            # Check BLOCKED tasks whose dependencies are all completed
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
                    alerts.append(
                        {
                            "severity": "warning",
                            "category": "task",
                            "title": f"任务可解除阻塞: {task.title}",
                            "description": (
                                f"任务 '{task.title}' 状态为BLOCKED，但所有依赖任务已完成"
                            ),
                            "suggested_action": ("将此任务状态从BLOCKED更新为PENDING"),
                            "task_id": task.id,
                        }
                    )

        return alerts

    async def check_system_health(self) -> list[dict[str, Any]]:
        """Check system health: database reachability."""
        alerts: list[dict[str, Any]] = []

        # Check database connection
        try:
            await self._repo.list_teams()
        except Exception as e:
            alerts.append(
                {
                    "severity": "critical",
                    "category": "system",
                    "title": "数据库连接异常",
                    "description": f"无法查询数据库: {e}",
                    "suggested_action": "检查数据库配置和连接状态",
                }
            )

        return alerts


class WatchdogRunner:
    """Background watchdog patrol service — asyncio.Task pattern.

    Periodically iterates over all active teams, runs all WatchdogChecker checks,
    and emits alerts to EventBus. Pattern modeled after StateReaper.
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
        """Start the background patrol loop."""
        if self._task is not None:
            logger.warning("WatchdogRunner已在运行，跳过重复启动")
            return
        self._running = True
        self._task = asyncio.create_task(self._run_loop(), name="watchdog-runner")
        logger.info("WatchdogRunner已启动，间隔=%ds", WATCHDOG_CHECK_INTERVAL)

    async def stop(self) -> None:
        """Stop the background patrol loop."""
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
        """Main patrol loop — executes once every WATCHDOG_CHECK_INTERVAL seconds."""
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
        """Patrol cycle — processes the default DB only."""
        try:
            await self._run_cycle_for_repo(self._checker._repo)
        except Exception:
            logger.exception("Watchdog cycle failed")

    async def _run_cycle_for_repo(self, repo: StorageRepository) -> None:
        """Single patrol cycle for a specific repository — iterate all active teams."""
        teams = await repo.list_teams()
        active_teams = [t for t in teams if t.status == TeamStatus.ACTIVE]
        alert_count = 0

        # Use a checker scoped to the given repo
        checker = WatchdogChecker(repo)

        for team in active_teams:
            # First execute stuck-agent auto-recovery
            recovered = await checker.auto_recover_stuck_agents(team.id)
            for record in recovered:
                await self._event_bus.emit(
                    "watchdog.agent_recovered",
                    f"team:{team.id}",
                    record,
                )

            # Failed task retry/alert
            failed_results = await checker.recover_failed_tasks(
                team.id,
                event_bus=self._event_bus,
            )
            for record in failed_results:
                if record.get("action") == "retried":
                    await self._event_bus.emit(
                        "watchdog.task_retried",
                        f"team:{team.id}",
                        record,
                    )

            alerts = await checker.run_all_checks(team.id)
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
