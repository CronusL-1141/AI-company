"""AI Team OS — StateReaper 后台收割器.

定期检查并回收超时的Agent状态，防止BUSY僵尸。
设计原则：Cheap Checks First — 正常轮询只做datetime比较，
只在异常时才写DB/emit事件/WS广播。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

from aiteam.api.event_bus import EventBus
from aiteam.config.settings import (
    HOOK_SOURCE_TIMEOUT,
    MEETING_EXPIRY_HOURS,
    REAPER_CHECK_INTERVAL,
)
from aiteam.storage.repository import StorageRepository
from aiteam.types import AgentStatus, MeetingStatus

logger = logging.getLogger(__name__)


class StateReaper:
    """后台状态收割器 — 定期回收超时的BUSY agent."""

    def __init__(self, repo: StorageRepository, event_bus: EventBus) -> None:
        self._repo = repo
        self._event_bus = event_bus
        self._task: asyncio.Task | None = None
        self._running = False

    def start(self) -> None:
        """启动后台收割循环."""
        if self._task is not None:
            logger.warning("StateReaper已在运行，跳过重复启动")
            return
        self._running = True
        self._task = asyncio.create_task(self._reap_loop(), name="state-reaper")
        logger.info("StateReaper已启动，间隔=%ds", REAPER_CHECK_INTERVAL)

    async def stop(self) -> None:
        """停止后台收割循环."""
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
            logger.info("StateReaper已停止")

    async def _reap_loop(self) -> None:
        """收割主循环 — 每REAPER_CHECK_INTERVAL秒执行一次."""
        while self._running:
            try:
                # 30秒硬超时保护，防止单次收割卡死
                await asyncio.wait_for(self._reap_cycle(), timeout=30.0)
            except TimeoutError:
                logger.warning("收割周期超时（30s），跳过本轮")
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("收割周期异常")

            try:
                await asyncio.sleep(REAPER_CHECK_INTERVAL)
            except asyncio.CancelledError:
                break

    async def _reap_cycle(self) -> None:
        """核心收割逻辑 — 遍历所有团队的BUSY agent检查超时."""
        now = datetime.now()
        teams = await self._repo.list_teams()
        reaped_count = 0

        for team in teams:
            agents = await self._repo.list_agents(team.id)

            for agent in agents:
                if agent.status == AgentStatus.BUSY:
                    # BUSY agent超时检查
                    if agent.source == "hook":
                        reaped = await self._check_hook_agent(agent, now)
                    else:
                        # api-source: 通过团队文件探测
                        reaped = await self._check_leader_via_team_files(agent, now)
                    if reaped:
                        reaped_count += 1

                # 不再做反向恢复（IDLE→BUSY），状态恢复由hooks驱动

        # 检查会议过期
        await self._check_meeting_expiry(now)

        # 检查活跃团队是否应该自动关闭（无活跃agent超过30分钟）
        await self._check_stale_teams(now)

        if reaped_count > 0:
            logger.warning("本轮收割了 %d 个超时agent", reaped_count)
        else:
            logger.debug("收割周期完成，无超时agent")

        await self._check_loop_auto_advance()

    async def _check_hook_agent(self, agent, now: datetime) -> bool:
        """检查hook-source agent是否心跳超时.

        判断依据：last_active_at距今是否超过HOOK_SOURCE_TIMEOUT（5分钟）。
        超时直接设为offline（心跳模式：Stop事件只做心跳，不改状态，
        超时才是真正的状态变更触发器）。
        """
        if agent.last_active_at is None:
            # 没有活动记录，用created_at作为基准
            reference_time = agent.created_at
        else:
            reference_time = agent.last_active_at

        elapsed = (now - reference_time).total_seconds()
        if elapsed <= HOOK_SOURCE_TIMEOUT:
            return False

        # 心跳超时 → 设为OFFLINE
        logger.warning(
            "hook-agent心跳超时: %s (team=%s), 已%.0f秒无活动，设为OFFLINE",
            agent.name, agent.team_id, elapsed,
        )
        await self._repo.update_agent(
            agent.id, status=AgentStatus.OFFLINE.value, current_task=None,
        )
        await self._event_bus.emit(
            "agent.status_changed",
            f"agent:{agent.id}",
            {
                "agent_id": agent.id,
                "name": agent.name,
                "old_status": "busy",
                "status": "offline",
                "trigger": "heartbeat_timeout",
                "elapsed_seconds": round(elapsed),
            },
        )
        return True

    async def _check_leader_via_team_files(self, agent, now: datetime) -> bool:
        """检查api-source BUSY agent是否超时.

        仅对BUSY的api-source agent调用。
        基于last_active_at判断，不再依赖团队文件探测。
        """
        if agent.last_active_at is None:
            reference_time = agent.created_at
        else:
            reference_time = agent.last_active_at

        from aiteam.config.settings import API_SOURCE_TIMEOUT_NO_FILE

        elapsed = (now - reference_time).total_seconds()
        if elapsed <= API_SOURCE_TIMEOUT_NO_FILE:
            return False

        # 超时 → 设为WAITING
        logger.warning(
            "api-agent超时: %s, 已%.0f秒无活动，设为WAITING",
            agent.name, elapsed,
        )
        await self._repo.update_agent(
            agent.id, status=AgentStatus.WAITING.value, current_task=None,
        )
        await self._event_bus.emit(
            "agent.status_changed",
            f"agent:{agent.id}",
            {
                "agent_id": agent.id,
                "name": agent.name,
                "old_status": "busy",
                "status": "waiting",
                "trigger": "timeout_reaper",
                "elapsed_seconds": round(elapsed),
            },
        )
        return True

    async def _check_stale_teams(self, now: datetime) -> None:
        """检查活跃团队是否应自动关闭.

        条件：团队内所有agent都是offline/waiting且最后活跃超过30分钟。
        同时检测CC团队配置文件是否已删除（CC TeamDelete后OS应跟随关闭）。
        """
        import os
        from pathlib import Path

        stale_threshold = now - timedelta(minutes=30)
        teams_dir = Path.home() / ".claude" / "teams"

        teams = await self._repo.list_teams()
        for team in teams:
            if team.status != "active":
                continue

            agents = await self._repo.list_agents(team.id)
            if not agents:
                # 空团队超过30分钟关闭
                if team.created_at and team.created_at < stale_threshold:
                    await self._repo.update_team(team.id, status="completed")
                    logger.info("StateReaper: 关闭空团队 '%s'", team.name)
                continue

            # 检查是否所有agent都非活跃
            has_active = False
            latest_activity = None
            for agent in agents:
                if agent.status == "busy":
                    has_active = True
                    break
                if agent.last_active_at:
                    if latest_activity is None or agent.last_active_at > latest_activity:
                        latest_activity = agent.last_active_at

            if has_active:
                continue

            # 所有agent非busy，检查最后活跃时间
            if latest_activity and latest_activity < stale_threshold:
                # 额外检查：CC团队配置文件是否还存在
                cc_team_dir = teams_dir / team.name.lower().replace(" ", "-")
                cc_config = cc_team_dir / "config.json"
                if not cc_config.exists():
                    # CC团队已删除，关闭OS团队
                    await self._repo.update_team(team.id, status="completed")
                    for agent in agents:
                        if agent.status != "offline":
                            await self._repo.update_agent(agent.id, status="offline")
                    logger.info(
                        "StateReaper: CC团队已删除，关闭OS团队 '%s'（%d agents→offline）",
                        team.name, len(agents),
                    )

    async def _check_loop_auto_advance(self) -> None:
        """检查Loop是否可以自动推进到下一阶段."""
        from aiteam.loop.engine import LoopEngine
        from aiteam.types import TaskStatus

        engine = LoopEngine(self._repo)
        teams = await self._repo.list_teams()

        for team in teams:
            if team.status != "active":
                continue
            try:
                state = await engine.get_state(team.id)
            except Exception:
                logger.exception("Loop自动推进获取状态失败: team=%s", team.id)
                continue
            if not state or not state.phase:
                continue

            phase = state.phase if isinstance(state.phase, str) else state.phase.value

            try:
                # EXECUTING → 检查任务完成情况
                if phase == "executing":
                    running = await self._repo.list_tasks(team.id, status=TaskStatus.RUNNING)
                    pending = await self._repo.list_tasks(team.id, status=TaskStatus.PENDING)
                    if not running and not pending:
                        await engine.advance(team.id, "all_tasks_done")
                        logger.info("Loop自动推进: %s EXECUTING→REVIEWING", team.id)
                    elif not running and pending:
                        await engine.advance(team.id, "batch_completed")
                        logger.info("Loop自动推进: %s EXECUTING→MONITORING", team.id)

                # MONITORING → 推进到REVIEWING
                elif phase == "monitoring":
                    await engine.advance(team.id, "all_clear")
                    logger.info("Loop自动推进: %s MONITORING→REVIEWING", team.id)

                # REVIEWING → 检查是否有新任务
                elif phase == "reviewing":
                    pending = await self._repo.list_tasks(team.id, status=TaskStatus.PENDING)
                    if pending:
                        await engine.advance(team.id, "new_tasks_added")
                        logger.info("Loop自动推进: %s REVIEWING→PLANNING", team.id)

            except Exception:
                logger.exception("Loop自动推进失败: team=%s, phase=%s", team.id, phase)

    async def _check_meeting_expiry(self, now: datetime) -> None:
        """检查并自动结束超期会议.

        活跃会议超过MEETING_EXPIRY_HOURS小时无新消息自动conclude。
        """
        expiry_threshold = now - timedelta(hours=MEETING_EXPIRY_HOURS)
        teams = await self._repo.list_teams()

        for team in teams:
            meetings = await self._repo.list_meetings(
                team.id, status=MeetingStatus.ACTIVE,
            )
            for meeting in meetings:
                # 获取会议消息，取最新一条的时间
                # list_meeting_messages按timestamp ASC排序，取最后一条
                messages = await self._repo.list_meeting_messages(
                    meeting.id,
                )
                if messages:
                    last_msg_time = messages[-1].timestamp
                else:
                    # 无消息，用会议创建时间
                    last_msg_time = meeting.created_at

                if last_msg_time < expiry_threshold:
                    logger.warning(
                        "会议过期: %s (topic=%s), 最后消息于 %s，自动结束",
                        meeting.id, meeting.topic, last_msg_time,
                    )
                    await self._repo.update_meeting(
                        meeting.id,
                        status=MeetingStatus.CONCLUDED.value,
                        concluded_at=now,
                    )
                    await self._event_bus.emit(
                        "meeting.concluded",
                        f"meeting:{meeting.id}",
                        {
                            "meeting_id": meeting.id,
                            "topic": meeting.topic,
                            "team_id": team.id,
                            "trigger": "expiry_reaper",
                            "hours_inactive": round(
                                (now - last_msg_time).total_seconds() / 3600, 1,
                            ),
                        },
                    )
