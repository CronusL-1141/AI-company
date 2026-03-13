"""AI Team OS — StateReaper 后台收割器.

定期检查并回收超时的Agent状态，防止BUSY僵尸。
设计原则：Cheap Checks First — 正常轮询只做datetime比较，
只在异常时才写DB/emit事件/WS广播。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from pathlib import Path

from aiteam.api.event_bus import EventBus
from aiteam.config.settings import (
    CLAUDE_HOME,
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
            except asyncio.TimeoutError:
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

                elif agent.status == AgentStatus.IDLE and agent.source == "api":
                    # IDLE的api-source agent: 双向修正（团队文件在但agent IDLE）
                    await self._try_recover_from_team_files(agent)

        # 检查会议过期
        await self._check_meeting_expiry(now)

        if reaped_count > 0:
            logger.warning("本轮收割了 %d 个超时agent", reaped_count)
        else:
            logger.debug("收割周期完成，无超时agent")

    async def _check_hook_agent(self, agent, now: datetime) -> bool:
        """检查hook-source agent是否超时.

        判断依据：last_active_at距今是否超过HOOK_SOURCE_TIMEOUT。
        """
        if agent.last_active_at is None:
            # 没有活动记录，用created_at作为基准
            reference_time = agent.created_at
        else:
            reference_time = agent.last_active_at

        elapsed = (now - reference_time).total_seconds()
        if elapsed <= HOOK_SOURCE_TIMEOUT:
            return False

        # 超时 → 设为IDLE
        logger.warning(
            "hook-agent超时: %s (team=%s), 已%.0f秒无活动，设为IDLE",
            agent.name, agent.team_id, elapsed,
        )
        await self._repo.update_agent(
            agent.id, status=AgentStatus.IDLE.value, current_task=None,
        )
        await self._event_bus.emit(
            "agent.status_changed",
            f"agent:{agent.id}",
            {
                "agent_id": agent.id,
                "name": agent.name,
                "old_status": "busy",
                "status": "idle",
                "trigger": "timeout_reaper",
                "elapsed_seconds": round(elapsed),
            },
        )
        return True

    async def _check_leader_via_team_files(self, agent, now: datetime) -> bool:
        """CC团队文件探测 — 检查~/.claude/teams/目录判断Leader活跃度.

        仅对BUSY的api-source agent调用。
        - 有活跃团队文件 → 活跃，不收割
        - 无团队文件 → 检查last_active_at超时，超时则设IDLE
        """
        has_team_files = self._detect_team_files()

        if has_team_files:
            # 有文件 = 活跃，不需要收割
            return False

        # 无团队文件 → 检查last_active_at
        if agent.last_active_at is None:
            reference_time = agent.created_at
        else:
            reference_time = agent.last_active_at

        from aiteam.config.settings import API_SOURCE_TIMEOUT_NO_FILE

        elapsed = (now - reference_time).total_seconds()
        if elapsed <= API_SOURCE_TIMEOUT_NO_FILE:
            return False

        # 超时且无团队文件 → 设为IDLE
        logger.warning(
            "api-agent超时: %s (无团队文件), 已%.0f秒无活动，设为IDLE",
            agent.name, elapsed,
        )
        await self._repo.update_agent(
            agent.id, status=AgentStatus.IDLE.value, current_task=None,
        )
        await self._event_bus.emit(
            "agent.status_changed",
            f"agent:{agent.id}",
            {
                "agent_id": agent.id,
                "name": agent.name,
                "old_status": "busy",
                "status": "idle",
                "trigger": "team_file_check",
                "elapsed_seconds": round(elapsed),
            },
        )
        return True

    async def _try_recover_from_team_files(self, agent) -> None:
        """双向修正 — 如果团队文件存在但agent是IDLE，修正为BUSY.

        仅对IDLE的api-source agent调用。
        """
        if not self._detect_team_files():
            return

        logger.warning(
            "团队文件修正: %s 应为BUSY（检测到活跃团队文件），修正状态",
            agent.name,
        )
        await self._repo.update_agent(
            agent.id, status=AgentStatus.BUSY.value,
        )
        await self._event_bus.emit(
            "agent.status_changed",
            f"agent:{agent.id}",
            {
                "agent_id": agent.id,
                "name": agent.name,
                "old_status": "idle",
                "status": "busy",
                "trigger": "team_file_recovery",
            },
        )

    def _detect_team_files(self) -> bool:
        """检查~/.claude/teams/目录下是否有与当前项目相关的活跃团队.

        读取config.json验证CWD匹配当前项目，排除其他项目的旧团队文件。
        """
        import json
        import os

        claude_home = Path(CLAUDE_HOME).expanduser()
        teams_dir = claude_home / "teams"

        if not teams_dir.exists():
            return False

        # 当前项目目录（用于匹配）
        current_cwd = os.getcwd().replace("\\", "/").lower()

        try:
            for entry in teams_dir.iterdir():
                if not entry.is_dir():
                    continue
                config_file = entry / "config.json"
                if not config_file.exists():
                    continue
                try:
                    config = json.loads(config_file.read_text(encoding="utf-8"))
                    # 检查members中是否有CWD匹配当前项目的成员
                    for member in config.get("members", []):
                        member_cwd = member.get("cwd", "").replace("\\", "/").lower()
                        if member_cwd and member_cwd == current_cwd:
                            return True
                except (json.JSONDecodeError, OSError):
                    continue
        except OSError:
            logger.debug("读取teams目录失败: %s", teams_dir)

        return False

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
