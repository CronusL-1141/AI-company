"""AI Team OS — StateReaper background harvester.

Periodically checks and reclaims timed-out Agent states to prevent BUSY zombies.
Design principle: Cheap Checks First — normal polling only does datetime comparisons,
DB writes/event emissions/WS broadcasts only happen on anomalies.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

from aiteam.api.event_bus import EventBus
from aiteam.config.settings import (
    HOOK_SOURCE_TIMEOUT,
    MEETING_EXPIRY_MINUTES,
    REAPER_CHECK_INTERVAL,
)
from aiteam.storage.repository import StorageRepository
from aiteam.types import AgentStatus, MeetingStatus

logger = logging.getLogger(__name__)


class StateReaper:
    """Background state reaper — periodically reclaims timed-out BUSY agents."""

    def __init__(self, repo: StorageRepository, event_bus: EventBus) -> None:
        self._repo = repo
        self._event_bus = event_bus
        self._task: asyncio.Task | None = None
        self._running = False

    def start(self) -> None:
        """Start background reaping loop."""
        if self._task is not None:
            logger.warning("StateReaper already running, skipping duplicate start")
            return
        self._running = True
        self._task = asyncio.create_task(self._reap_loop(), name="state-reaper")
        logger.info("StateReaper started, interval=%ds", REAPER_CHECK_INTERVAL)

    async def stop(self) -> None:
        """Stop background reaping loop."""
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
            logger.info("StateReaper stopped")

    async def _reap_loop(self) -> None:
        """Main reaping loop — executes every REAPER_CHECK_INTERVAL seconds."""
        while self._running:
            try:
                # 30s hard timeout protection against single cycle hangs
                await asyncio.wait_for(self._reap_cycle(), timeout=30.0)
            except TimeoutError:
                logger.warning("Reap cycle timed out (30s), skipping this round")
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Reap cycle exception")

            try:
                await asyncio.sleep(REAPER_CHECK_INTERVAL)
            except asyncio.CancelledError:
                break

    async def _reap_cycle(self) -> None:
        """Core reaping logic — iterates all teams' BUSY agents checking for timeouts."""
        now = datetime.now()
        teams = await self._repo.list_teams()
        reaped_count = 0

        for team in teams:
            agents = await self._repo.list_agents(team.id)

            for agent in agents:
                if agent.status == AgentStatus.BUSY:
                    # BUSY agent timeout check
                    if agent.source == "hook":
                        reaped = await self._check_hook_agent(agent, now)
                    else:
                        # api-source: probe via team files
                        reaped = await self._check_leader_via_team_files(agent, now)
                    if reaped:
                        reaped_count += 1

                # No reverse recovery (IDLE->BUSY); state recovery is driven by hooks

        # Check meeting expiry
        await self._check_meeting_expiry(now)

        # Immediately detect CC-deleted teams (don't wait 30 minutes)
        await self._check_team_liveness()

        # Check if active teams should be auto-closed (no active agents for >30 minutes)
        await self._check_stale_teams(now)

        if reaped_count > 0:
            logger.warning("Reaped %d timed-out agents this cycle", reaped_count)
        else:
            logger.debug("Reap cycle complete, no timed-out agents")

        await self._check_agent_liveness()
        await self._check_loop_auto_advance()
        await self._check_scheduled_tasks(now)

    async def _check_hook_agent(self, agent, now: datetime) -> bool:
        """Check if a hook-source agent has heartbeat timeout.

        Criterion: whether last_active_at exceeds HOOK_SOURCE_TIMEOUT (5 minutes).
        Timeout sets agent to offline (heartbeat mode: Stop events only refresh heartbeat,
        don't change status; timeout is the real state change trigger).
        """
        if agent.last_active_at is None:
            # No activity record, use created_at as baseline
            reference_time = agent.created_at
        else:
            reference_time = agent.last_active_at

        elapsed = (now - reference_time).total_seconds()
        if elapsed <= HOOK_SOURCE_TIMEOUT:
            return False

        # Heartbeat timeout -> set to OFFLINE
        logger.warning(
            "Hook-agent heartbeat timeout: %s (team=%s), %.0fs inactive, setting to OFFLINE",
            agent.name,
            agent.team_id,
            elapsed,
        )
        await self._repo.update_agent(
            agent.id,
            status=AgentStatus.OFFLINE.value,
            current_task=None,
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
        """Check if an api-source BUSY agent has timed out.

        Only called for BUSY api-source agents.
        Based on last_active_at; no longer relies on team file probing.
        """
        if agent.last_active_at is None:
            reference_time = agent.created_at
        else:
            reference_time = agent.last_active_at

        from aiteam.config.settings import API_SOURCE_TIMEOUT_NO_FILE

        elapsed = (now - reference_time).total_seconds()
        if elapsed <= API_SOURCE_TIMEOUT_NO_FILE:
            return False

        # Timeout -> set to WAITING
        logger.warning(
            "Api-agent timeout: %s, %.0fs inactive, setting to WAITING",
            agent.name,
            elapsed,
        )
        await self._repo.update_agent(
            agent.id,
            status=AgentStatus.WAITING.value,
            current_task=None,
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

    async def _check_team_liveness(self) -> None:
        """Immediately detect CC-deleted teams and sync-close OS teams.

        Unlike _check_stale_teams, this method doesn't wait 30 minutes;
        it closes immediately when CC config disappears.
        Applies when user executes TeamDelete and OS needs to sync quickly.
        """
        from pathlib import Path

        teams_dir = Path.home() / ".claude" / "teams"
        if not teams_dir.exists():
            return

        # Collect all existing CC team directory names (for matching)
        existing_cc_dirs: set[str] = set()
        for entry in teams_dir.iterdir():
            if entry.is_dir() and (entry / "config.json").exists():
                existing_cc_dirs.add(entry.name)

        teams = await self._repo.list_teams()
        for team in teams:
            if team.status != "active":
                continue

            # Convert OS team name to CC directory name (consistent with _check_stale_teams)
            cc_dir_name = team.name.lower().replace(" ", "-")
            if cc_dir_name in existing_cc_dirs:
                continue  # CC team still alive, skip

            # CC team config missing -> immediately close OS team
            agents = await self._repo.list_agents(team.id)
            await self._repo.update_team(team.id, status="completed")
            for agent in agents:
                if agent.status != "offline":
                    await self._repo.update_agent(
                        agent.id,
                        status="offline",
                        current_task=None,
                    )
            # Cascade: conclude all active meetings for this team
            from datetime import datetime as dt

            concluded = await self._conclude_team_meetings(team.id, dt.now(), "team_closed")
            await self._event_bus.emit(
                "team.status_changed",
                f"team:{team.id}",
                {
                    "team_id": team.id,
                    "name": team.name,
                    "status": "completed",
                    "trigger": "team_liveness",
                    "agents_offline": len(agents),
                    "meetings_concluded": concluded,
                },
            )
            logger.info(
                "Config probe: CC team '%s' closed (%d offline, %d meetings)",
                team.name,
                len(agents),
                concluded,
            )

    async def _check_stale_teams(self, now: datetime) -> None:
        """Check if active teams should be auto-closed.

        Conditions: all agents are offline/waiting and last active >30 minutes ago.
        Also detects whether CC team config files have been deleted
        (OS should follow suit after CC TeamDelete).
        """
        from pathlib import Path

        stale_threshold = now - timedelta(minutes=30)
        teams_dir = Path.home() / ".claude" / "teams"

        teams = await self._repo.list_teams()
        for team in teams:
            if team.status != "active":
                continue

            agents = await self._repo.list_agents(team.id)
            if not agents:
                # Empty team older than 30 minutes -> close
                if team.created_at and team.created_at < stale_threshold:
                    await self._repo.update_team(team.id, status="completed")
                    logger.info("StateReaper: closed empty team '%s'", team.name)
                continue

            # Check if all agents are inactive
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

            # All agents non-busy, check last activity time
            if latest_activity and latest_activity < stale_threshold:
                # Extra check: does CC team config file still exist?
                cc_team_dir = teams_dir / team.name.lower().replace(" ", "-")
                cc_config = cc_team_dir / "config.json"
                if not cc_config.exists():
                    # CC team deleted, close OS team + cascade meetings
                    await self._repo.update_team(team.id, status="completed")
                    for agent in agents:
                        if agent.status != "offline":
                            await self._repo.update_agent(agent.id, status="offline")
                    from datetime import datetime as dt

                    concluded = await self._conclude_team_meetings(team.id, dt.now(), "stale_team_closed")
                    logger.info(
                        "StateReaper: team '%s' closed (%d offline, %d meetings)",
                        team.name,
                        len(agents),
                        concluded,
                    )

    async def _check_loop_auto_advance(self) -> None:
        """Check if Loop can auto-advance to next phase."""
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
                logger.exception("Loop auto-advance get_state failed: team=%s", team.id)
                continue
            if not state or not state.phase:
                continue

            phase = state.phase if isinstance(state.phase, str) else state.phase.value

            try:
                # EXECUTING -> check task completion
                if phase == "executing":
                    running = await self._repo.list_tasks(team.id, status=TaskStatus.RUNNING)
                    pending = await self._repo.list_tasks(team.id, status=TaskStatus.PENDING)
                    if not running and not pending:
                        await engine.advance(team.id, "all_tasks_done")
                        logger.info("Loop auto-advance: %s EXECUTING->REVIEWING", team.id)
                    elif not running and pending:
                        await engine.advance(team.id, "batch_completed")
                        logger.info("Loop auto-advance: %s EXECUTING->MONITORING", team.id)

                # MONITORING -> advance to REVIEWING
                elif phase == "monitoring":
                    await engine.advance(team.id, "all_clear")
                    logger.info("Loop auto-advance: %s MONITORING->REVIEWING", team.id)

                # REVIEWING -> check for new tasks
                elif phase == "reviewing":
                    pending = await self._repo.list_tasks(team.id, status=TaskStatus.PENDING)
                    if pending:
                        await engine.advance(team.id, "new_tasks_added")
                        logger.info("Loop auto-advance: %s REVIEWING->PLANNING", team.id)

            except Exception:
                logger.exception("Loop auto-advance failed: team=%s, phase=%s", team.id, phase)

    async def _check_agent_liveness(self) -> None:
        """Detect agent liveness based on CC team config."""
        import json as _json
        from pathlib import Path

        teams_dir = Path.home() / ".claude" / "teams"
        if not teams_dir.exists():
            return

        # 1. Collect all active member names from CC team configs
        alive_names: set[str] = set()
        for team_dir in teams_dir.iterdir():
            if not team_dir.is_dir():
                continue
            config_path = team_dir / "config.json"
            if not config_path.exists():
                continue
            try:
                data = _json.loads(config_path.read_text(encoding="utf-8"))
                for member in data.get("members", []):
                    name = member.get("name", "")
                    if name:
                        alive_names.add(name)
            except Exception:
                continue

        # 2. Check if busy/waiting hook agents in OS are still alive
        teams = await self._repo.list_teams()
        for team in teams:
            if team.status != "active":
                continue
            agents = await self._repo.list_agents(team.id)
            for agent in agents:
                if agent.source != "hook" or agent.status == "offline":
                    continue
                # team-lead managed by SessionStart/SessionEnd, skip
                if agent.name == "team-lead":
                    continue
                # busy/waiting agent not in any team config -> offline
                if agent.name not in alive_names:
                    await self._repo.update_agent(
                        agent.id,
                        status=AgentStatus.OFFLINE.value,
                        current_task=None,
                    )
                    await self._event_bus.emit(
                        "agent.status_changed",
                        f"agent:{agent.id}",
                        {
                            "agent_id": agent.id,
                            "name": agent.name,
                            "status": "offline",
                            "trigger": "config_liveness",
                        },
                    )
                    logger.info(
                        "Config probe: %s not in CC team members -> offline",
                        agent.name,
                    )

    async def _conclude_team_meetings(self, team_id: str, now: datetime, trigger: str) -> int:
        """Conclude all active meetings for a team. Returns count of concluded meetings."""
        meetings = await self._repo.list_meetings(team_id, status=MeetingStatus.ACTIVE)
        count = 0
        for meeting in meetings:
            await self._repo.update_meeting(meeting.id, status=MeetingStatus.CONCLUDED.value, concluded_at=now)
            await self._event_bus.emit(
                "meeting.concluded",
                f"meeting:{meeting.id}",
                {"meeting_id": meeting.id, "topic": meeting.topic, "team_id": team_id, "trigger": trigger},
            )
            count += 1
        if count:
            logger.info("Auto-concluded %d meeting(s) for team %s (trigger=%s)", count, team_id, trigger)
        return count

    async def _check_meeting_expiry(self, now: datetime) -> None:
        """Check and auto-conclude expired meetings.

        Active meetings with no new messages for MEETING_EXPIRY_MINUTES are auto-concluded.
        """
        expiry_threshold = now - timedelta(minutes=MEETING_EXPIRY_MINUTES)
        teams = await self._repo.list_teams()

        for team in teams:
            meetings = await self._repo.list_meetings(
                team.id,
                status=MeetingStatus.ACTIVE,
            )
            for meeting in meetings:
                # Get meeting messages, take the latest one's timestamp
                # list_meeting_messages sorts by timestamp ASC, take the last one
                messages = await self._repo.list_meeting_messages(
                    meeting.id,
                )
                if messages:
                    last_msg_time = messages[-1].timestamp
                else:
                    # No messages, use meeting creation time
                    last_msg_time = meeting.created_at

                if last_msg_time < expiry_threshold:
                    logger.warning(
                        "Meeting expired: %s (topic=%s), last message at %s, auto-concluding",
                        meeting.id,
                        meeting.topic,
                        last_msg_time,
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
                            "minutes_inactive": round(
                                (now - last_msg_time).total_seconds() / 60,
                                1,
                            ),
                        },
                    )

    async def _check_scheduled_tasks(self, now: datetime) -> None:
        """Execute due scheduled tasks.

        For each due task:
        - If past-due > 1 hour: skip (treat as missed, don't pile up)
        - Execute action based on action_type
        - Update last_run_at and next_run_at regardless of action success
        - Each task has independent try/except so one failure won't block others
        """
        due_tasks = await self._repo.get_due_tasks(now)
        if not due_tasks:
            return

        one_hour = timedelta(hours=1)

        for sched_task in due_tasks:
            try:
                overdue = now - sched_task.next_run_at
                if overdue > one_hour:
                    logger.info(
                        "Scheduled task '%s' is past-due by %.0f min, skipping",
                        sched_task.name,
                        overdue.total_seconds() / 60,
                    )
                    # Still advance next_run_at so it doesn't keep triggering
                    next_run = now + timedelta(seconds=sched_task.interval_seconds)
                    await self._repo.update_scheduled_task(
                        sched_task.id,
                        last_run_at=now,
                        next_run_at=next_run,
                    )
                    continue

                await self._execute_scheduled_action(sched_task, now)

                next_run = now + timedelta(seconds=sched_task.interval_seconds)
                await self._repo.update_scheduled_task(
                    sched_task.id,
                    last_run_at=now,
                    next_run_at=next_run,
                )
                logger.info(
                    "Scheduled task '%s' executed (action=%s), next_run=%s",
                    sched_task.name,
                    sched_task.action_type,
                    next_run.isoformat(),
                )
            except Exception:
                logger.exception("Scheduled task '%s' failed", sched_task.name)

    async def _execute_scheduled_action(self, sched_task, now: datetime) -> None:
        """Dispatch a scheduled task's action."""
        cfg = sched_task.action_config or {}
        action = sched_task.action_type

        if action == "create_task":
            title = cfg.get("title", sched_task.name)
            description = cfg.get("description", sched_task.description)
            priority = cfg.get("priority", "medium")
            team_id = sched_task.team_id
            await self._repo.create_task(
                team_id=team_id,
                title=title,
                description=description,
                priority=priority,
            )
            await self._event_bus.emit(
                "task.created",
                f"scheduler:{sched_task.id}",
                {
                    "trigger": "scheduler",
                    "scheduled_task_id": sched_task.id,
                    "scheduled_task_name": sched_task.name,
                    "title": title,
                    "team_id": team_id,
                },
            )

        elif action == "inject_reminder":
            message = cfg.get("message", sched_task.description or sched_task.name)
            await self._event_bus.emit(
                "scheduler.reminder",
                f"scheduler:{sched_task.id}",
                {
                    "trigger": "scheduler",
                    "scheduled_task_id": sched_task.id,
                    "scheduled_task_name": sched_task.name,
                    "message": message,
                    "team_id": sched_task.team_id,
                    "timestamp": now.isoformat(),
                },
            )

        elif action == "emit_event":
            event_type = cfg.get("event_type", "scheduler.custom")
            event_data = cfg.get("data", {})
            await self._event_bus.emit(
                event_type,
                f"scheduler:{sched_task.id}",
                {
                    "trigger": "scheduler",
                    "scheduled_task_id": sched_task.id,
                    "scheduled_task_name": sched_task.name,
                    "team_id": sched_task.team_id,
                    **event_data,
                },
            )

        else:
            logger.warning(
                "Unknown scheduled action type '%s' for task '%s'",
                action,
                sched_task.name,
            )
