"""AI Team OS — StateReaper background harvester.

Periodically checks and reclaims timed-out Agent states to prevent BUSY zombies.
Design principle: Cheap Checks First — normal polling only does datetime comparisons,
DB writes/event emissions/WS broadcasts only happen on anomalies.

Multi-DB support: each reap cycle scans all per-project databases in addition to the
default database. Each project DB is processed independently so a single failure does
not block others (error isolation).
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta

from aiteam.api import agent_context
from aiteam.api.event_bus import EventBus
from aiteam.api.wake_manager import WakeAgentManager
from aiteam.config.settings import (
    CONTAINER_TEAM_PURGE_MAX_PER_CYCLE,
    CONTAINER_TEAM_RETENTION_DAYS,
    HOOK_SOURCE_TIMEOUT,
    MEETING_EXPIRY_MINUTES,
    REAPER_CHECK_INTERVAL,
    WORKFLOW_TEAM_NO_RUN_GRACE_HOURS,
)
from aiteam.storage.repository import StorageRepository
from aiteam.types import AgentStatus, MeetingStatus

logger = logging.getLogger(__name__)

# Workflow run terminal statuses — mirrors workflow_ingest._WF_TERMINAL_STATUSES.
# Used to decide when an adopted workflow-session fallback shell may be reclaimed
# (a terminal run means no more subagents will register into the shell).
_WORKFLOW_TERMINAL_STATUSES = frozenset({"completed", "killed", "failed"})

# Runs that will not advance by themselves any more: the terminal ones plus
# ``interrupted`` — the live tail's verdict that a run stopped producing journal
# entries. Used ONLY to judge whether a team has settled. The run-status ladder still
# treats interrupted as non-terminal (enrichment may promote it to completed later),
# and if that happens workflow_ingest re-opens the team it belongs to.
_WORKFLOW_SETTLED_STATUSES = _WORKFLOW_TERMINAL_STATUSES | {"interrupted"}


class StateReaper:
    """Background state reaper — periodically reclaims timed-out BUSY agents."""

    def __init__(self, repo: StorageRepository, event_bus: EventBus) -> None:
        self._repo = repo
        self._event_bus = event_bus
        self._task: asyncio.Task | None = None
        self._running = False
        self._wake_manager = WakeAgentManager(repo, event_bus)
        # D3 阶段C：治理 leader 租约持有者标识——同进程的 reaper/watchdog 共用
        # f"api-{pid}"，同进程两个治理循环互为续约、绝不互抢。
        self._lease_holder = f"api-{os.getpid()}"
        # 默认模型健康巡检节流与去重（2026-07-10 用户裁定 fable 额度回退）
        self._last_model_health_check: datetime | None = None
        self._model_health_notified = False
        # 存活判据双轨观察（C13）：session_id -> 上次记录分歧的时刻。
        # 只在两路判断不一致时落一条事件，且每会话 10 分钟一条——观察本身不能
        # 变成新的事件洪水（正是 Q2 要消灭的东西）。
        self._liveness_divergence_seen: dict[str, datetime] = {}

    @property
    def wake_manager(self) -> WakeAgentManager:
        """The WakeAgentManager instance (shared subprocess machine for wakes + fleet
        dispatch). Exposed so the /api/fleet/dispatch route can drive a ship through the
        same semaphore/circuit-breaker/ledger as scheduled wakes."""
        return self._wake_manager

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
        # 让出治理租约，新实例无需等满 TTL（180s）即可接管治理动作。
        # best-effort：失败只记日志，租约自身的 TTL 仍是兜底。
        try:
            if await self._repo.release_governance_lease(self._lease_holder):
                logger.info("Governance lease released by %s", self._lease_holder)
        except Exception:  # noqa: BLE001 — 关闭路径绝不因此抛出
            logger.debug("Governance lease release failed (TTL will expire it)")
        await self._wake_manager.shutdown()

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
        """Reap cycle — processes the default DB only."""
        # D3 阶段C：治理 leader 租约（审计 M50）——多 API 实例并存时仅租约持有者
        # 运行治理动作（回收/推进/调度/唤醒/对账），杜绝重复唤醒与双份治理。
        # 租约层故障时 fail-open：单实例场景无损，双实例退化为修复前行为。
        try:
            is_leader = await self._repo.try_acquire_governance_lease(
                self._lease_holder, ttl_seconds=REAPER_CHECK_INTERVAL * 3
            )
        except Exception:
            is_leader = True
        if not is_leader:
            logger.debug("Governance lease held by another instance — skipping reap cycle")
            return
        try:
            await self._reap_cycle_for_repo(self._repo)
        except Exception:
            logger.exception("Reap cycle failed")

    async def _reap_cycle_for_repo(self, repo: StorageRepository) -> None:
        """Core reaping logic for a single repository — iterates all teams' BUSY agents
        checking for timeouts.
        """
        now = datetime.now()
        teams = await repo.list_teams()
        reaped_count = 0

        for team in teams:
            agents = await repo.list_agents(team.id)

            for agent in agents:
                if agent.status == AgentStatus.BUSY:
                    # BUSY agent timeout check
                    if agent.source == "hook":
                        reaped = await self._check_hook_agent(agent, now, repo)
                    else:
                        # api-source: probe via team files
                        reaped = await self._check_leader_via_team_files(agent, now, repo)
                    if reaped:
                        reaped_count += 1

                # No reverse recovery (IDLE->BUSY); state recovery is driven by hooks

        # Check meeting expiry
        await self._check_meeting_expiry(now, repo)

        # Check if active teams should be auto-closed (no active agents for >30 minutes).
        # The old impatient sibling (_check_team_liveness, "close the second the CC
        # config dir vanishes") is retired — see the block comment above it.
        await self._check_stale_teams(now, repo)

        # Sweep up the husks the step above (and SessionEnd) leave behind.
        await self._purge_spent_session_containers(repo)

        # 默认模型健康巡检（每小时一次）
        await self._check_default_model_health(now, repo)

        if reaped_count > 0:
            logger.warning("Reaped %d timed-out agents this cycle", reaped_count)
        else:
            logger.debug("Reap cycle complete, no timed-out agents")

        await self._check_agent_liveness(repo)
        await self._backfill_agent_watermarks(repo)
        await self._check_scheduled_tasks(now, repo)
        # I3a: 保底轮询 Workflow 完成检测（与会话解耦的耐久工作马）。
        await self._check_workflow_ingest(repo)

        # Hourly cleanup of old wake sessions
        if now.minute == 0:
            try:
                deleted = await repo.cleanup_old_sessions(days=30)
                if deleted:
                    logger.info("Cleaned up %d old wake sessions", deleted)
            except Exception as e:
                logger.error("Failed to cleanup wake sessions: %s", e)

    async def _check_hook_agent(
        self, agent, now: datetime, repo: StorageRepository | None = None
    ) -> bool:
        """Check if a hook-source agent has heartbeat timeout.

        Criterion: whether last_active_at exceeds HOOK_SOURCE_TIMEOUT (5 minutes).
        Timeout sets agent to offline (heartbeat mode: Stop events only refresh heartbeat,
        don't change status; timeout is the real state change trigger).
        """
        _repo = repo if repo is not None else self._repo

        if agent.last_active_at is None:
            # No activity record, use created_at as baseline
            reference_time = agent.created_at
        else:
            reference_time = agent.last_active_at

        elapsed = (now - reference_time).total_seconds()
        # workflow 子 agent 常有远超 5 分钟的合法静默（实测健康 agent 工具间隔
        # p99=78s/max=174s，深读型任务更久），5 分钟心跳必误杀——放宽到与观测层
        # interrupted 阈值同源的 900s；保留兜底自愈（SubagentStop 丢失时仍能收尸）。
        timeout = HOOK_SOURCE_TIMEOUT
        if getattr(agent, "role", "") == "workflow-subagent":
            timeout = max(HOOK_SOURCE_TIMEOUT, 900)
        if elapsed <= timeout:
            return False

        # Heartbeat timeout -> set to OFFLINE
        logger.warning(
            "Hook-agent heartbeat timeout: %s (team=%s), %.0fs inactive, setting to OFFLINE",
            agent.name,
            agent.team_id,
            elapsed,
        )
        await _repo.update_agent(
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

    async def _check_leader_via_team_files(
        self, agent, now: datetime, repo: StorageRepository | None = None
    ) -> bool:
        """Check if an api-source BUSY agent has timed out.

        Only called for BUSY api-source agents.
        Based on last_active_at; no longer relies on team file probing.
        """
        _repo = repo if repo is not None else self._repo

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
        await _repo.update_agent(
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

    # ── _check_team_liveness 已整分支退役（2026-07-27 批 7）───────────────────
    # 它的存在理由写在原 docstring 里："用户执行 TeamDelete 后 OS 需要快速跟随关队"。
    # 该前提已两头消失：① CC v2.1.219 起 TeamDelete 工具不存在；② `team_create`
    # MCP 工具与 POST /api/teams 同批退役，队只由 hook 链创建。
    # 实测背书（本机库 254 支队）：kind 分布 = workflow 168 / session 80 / 无 kind 6，
    # 前两类本就在豁免名单内，也就是说这条分支唯一还能命中的只有 6 支历史遗留队——
    # 收益为零，风险却是"CC 没建目录就立刻关队"这一类误关（2026-07-08 关队事故同源）。
    # 关队能力并未消失：_check_stale_teams 保留了同一套判据（CC config 消失 → 关队 +
    # 级联收会），只是多等 30 分钟并同样带会议宽限保护——方向坚持"只放行不误杀"。

    # ── 默认模型健康巡检（2026-07-10 用户裁定：fable 额度回退自动化）─────────
    # 官方 fallbackModel 明确不管额度耗尽（仅 529 过载），额度场景 CC 直接阻塞。
    # OS 侧近似检测：默认模型为 fable/mythos 家族，且全部 transcript 已 N 天未
    # 出现该家族 → 大概率不在订阅额度内 → 自动回退层级别名 "opus"（随代际
    # 最新，不写死版本号）+ briefing 留痕。AITEAM_MODEL_AUTOFALLBACK=off 时
    # 只提醒不改配置。回退成功后默认模型不再是 fable → 天然不重复触发。
    _MODEL_HEALTH_INTERVAL_S = 3600
    _FABLE_MISSING_DAYS = 3

    async def _check_default_model_health(
        self, now: datetime, repo: StorageRepository | None = None
    ) -> None:
        if (
            self._last_model_health_check is not None
            and (now - self._last_model_health_check).total_seconds()
            < self._MODEL_HEALTH_INTERVAL_S
        ):
            return
        self._last_model_health_check = now
        _repo = repo if repo is not None else self._repo
        try:
            import time as _time

            from aiteam.api import model_discovery as md

            default = (md.read_default_model() or "").lower()
            if not any(k in default for k in ("fable", "mythos")):
                return
            # scan 扫全部 transcript ~1s，进线程池（M6 教训：勿阻塞事件循环）
            models = await asyncio.to_thread(md.scan_available_models)
            latest = max(
                (
                    float(m.get("last_seen_ts") or 0)
                    for m in models
                    if any(
                        k in str(m.get("model", "")).lower()
                        for k in ("fable", "mythos")
                    )
                ),
                default=0.0,
            )
            if latest and _time.time() - latest < self._FABLE_MISSING_DAYS * 86400:
                return  # fable 家族仍活跃，额度正常
            if self._model_health_notified:
                return  # 本进程已提醒/已处置过，不刷屏
            auto_on = os.environ.get(
                "AITEAM_MODEL_AUTOFALLBACK", "on"
            ).lower() not in ("off", "0", "false")
            if auto_on:
                res = await asyncio.to_thread(md.set_default_model, "opus")
                await _repo.create_briefing(
                    title="默认启动模型已自动回退到 opus",
                    description=(
                        f"检测到默认模型 {default!r} 在全部 CC transcript 中已超过 "
                        f"{self._FABLE_MISSING_DAYS} 天未出现（大概率不在订阅额度内）。"
                        f"已将 ~/.claude/settings.json 的 model 改为层级别名 opus"
                        f"（自动跟随最新版），原文件备份 settings.json.bak-aiteam。"
                        f"写入结果: {res}。如需关闭自动回退：AITEAM_MODEL_AUTOFALLBACK=off"
                    ),
                    urgency="high",
                )
                logger.warning(
                    "Default model auto-fallback: %s -> opus (%s)", default, res
                )
            else:
                await _repo.create_briefing(
                    title=f"默认模型 {default} 疑似已不可用",
                    description=(
                        f"该模型家族已 {self._FABLE_MISSING_DAYS} 天未出现在任何 "
                        f"transcript 中。自动回退已关闭（AITEAM_MODEL_AUTOFALLBACK=off），"
                        f"建议手动切换：Settings 页模型治理卡或 model_config_set('opus')。"
                    ),
                    urgency="high",
                )
            self._model_health_notified = True
        except Exception as exc:  # noqa: BLE001 — 健康巡检失败不影响主收割
            logger.debug("default model health check failed: %s", exc)

    async def _has_recent_active_meeting(
        self, team_id: str, now: datetime, repo: StorageRepository
    ) -> bool:
        """True when the team has an ACTIVE meeting touched within the expiry window.

        Meeting activity is the strongest "this team is still in use" signal there
        is — stronger than roster or CC-config probes — so every close path checks
        it before pulling the trigger.
        """
        meeting_grace = timedelta(minutes=MEETING_EXPIRY_MINUTES)
        active_meetings = await repo.list_meetings(team_id, status=MeetingStatus.ACTIVE)
        for meeting in active_meetings:
            messages = await repo.list_meeting_messages(meeting.id)
            last_time = messages[-1].timestamp if messages else meeting.created_at
            if last_time is not None and now - last_time < meeting_grace:
                return True
        return False

    async def _check_stale_teams(
        self, now: datetime, repo: StorageRepository | None = None
    ) -> None:
        """Check if active teams should be auto-closed.

        Conditions: all agents are offline/waiting and last active >30 minutes ago.
        Also detects whether CC team config files have been deleted
        (OS should follow suit after CC TeamDelete).
        """
        from pathlib import Path

        _repo = repo if repo is not None else self._repo

        stale_threshold = now - timedelta(minutes=30)
        teams_dir = Path.home() / ".claude" / "teams"

        teams = await _repo.list_teams()
        for team in teams:
            if team.status != "active":
                # 矛盾态自愈（2026-07-25 实锤 wenge：队 completed 却挂着 4 个 busy
                # 成员，前端按 active 筛队 → 整队工作全盲）。成员在干活是比任何
                # 会话探活都硬的"队还活着"证据：发现该矛盾立即复活队。覆盖存量
                # 错乱与任何未被预见的关队路径（防御性，不依赖上游全部改对）。
                if team.status == "completed":
                    busy_members = [
                        m for m in await _repo.list_agents(team.id) if m.status == "busy"
                    ]
                    if busy_members:
                        await _repo.update_team(team.id, status="active")
                        logger.warning(
                            "StateReaper: revived team '%s' — completed but %d busy member(s)",
                            team.name,
                            len(busy_members),
                        )
                continue

            # workflow 队豁免：成员靠 promote/收尸迁移懒到位，run 长跑期间队可能
            # 长期 0 成员或全 offline；其关闭由 ingest 按 run 终态跟随（2026-07-08）。
            # 豁免只覆盖「run 还没跑完」，跑完了必须有人收——见 _close_settled_workflow_team。
            cfg = team.config or {}
            if cfg.get("kind") == "workflow":
                await self._close_settled_workflow_team(
                    team, cfg, now, stale_threshold, _repo
                )
                continue

            if cfg.get("kind") == "session":
                # Session container team (fleet-layer §5): its lifecycle follows the
                # OWNING CC session's transcript file mtime (file truth source), not
                # ~/.claude/teams config (it never has one). Close only when the
                # session is truly dead — file gone or mtime stale beyond threshold —
                # AND no member is still busy. File mtime is preferred over process
                # liveness because `claude --resume` spins up a fresh process anyway.
                from aiteam.api import session_probe

                members = await _repo.list_agents(team.id)
                if any(m.status == "busy" for m in members):
                    continue  # session still working (leader/agents busy)
                owner_sid = cfg.get("owner_session_id") or ""
                root = ""
                if team.project_id:
                    proj = await _repo.get_project(team.project_id)
                    root = (proj.root_path or "") if proj else ""
                last_active = (
                    session_probe.session_last_active(root, owner_sid)
                    if (root and owner_sid)
                    else None
                )
                # Fall back to created_at when the file can't be resolved (unbound
                # project or missing owner) so an idle container still ages out.
                reference = last_active or team.created_at
                # 成员活性兜底（2026-07-25 实锤：wenge 4 个 agent 在跑却全不可见）。
                # owner_session_id 常常探不到 transcript——CC v2.1.219 的隐式团队名
                # 是内部 id（session-87500462 这类无对应会话文件），会话 resume 后
                # hook 的 session_id 也会换。此时 session_probe=None 便回退 created_at
                # 判死，把正在干活的整队连人带队打成 completed/offline，前端全盲。
                # 成员最近活跃时间不依赖任何会话 id 追踪，是最可靠的"队还活着"证据。
                member_latest = max(
                    (m.last_active_at for m in members if m.last_active_at),
                    default=None,
                )
                if member_latest is not None and (
                    reference is None or member_latest > reference
                ):
                    reference = member_latest
                if reference is not None and reference < stale_threshold:
                    await _repo.update_team(team.id, status="completed")
                    for m in members:
                        if m.status != "offline":
                            await _repo.update_agent(
                                m.id, status="offline", current_task=None
                            )
                    logger.info(
                        "StateReaper: closed dead session container '%s' (owner=%s)",
                        team.name,
                        owner_sid[:8] if owner_sid else "?",
                    )
                continue

            agents = await _repo.list_agents(team.id)
            if not agents:
                # Empty team older than 30 minutes -> close.
                # A live meeting still counts as "the team is in use": participants
                # are addressed by name and need not be registered agents, so an
                # empty roster is NOT evidence of abandonment (this guard used to
                # exist only on the retired _check_team_liveness path).
                if team.created_at and team.created_at < stale_threshold:
                    if await self._has_recent_active_meeting(team.id, now, _repo):
                        logger.debug(
                            "StateReaper: empty team '%s' has a live meeting, deferring",
                            team.name,
                        )
                        continue
                    await _repo.update_team(team.id, status="completed")
                    concluded = await self._conclude_team_meetings(
                        team.id, now, "stale_team_closed", _repo
                    )
                    logger.info(
                        "StateReaper: closed empty team '%s' (%d meetings)",
                        team.name,
                        concluded,
                    )
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
                    # Guard: skip if active meetings have recent messages
                    if await self._has_recent_active_meeting(team.id, now, _repo):
                        logger.debug(
                            "StateReaper: team '%s' stale but has active recent meetings, deferring",
                            team.name,
                        )
                        continue

                    # CC team deleted, close OS team + cascade meetings
                    await _repo.update_team(team.id, status="completed")
                    for agent in agents:
                        if agent.status != "offline":
                            await _repo.update_agent(agent.id, status="offline")
                    from datetime import datetime as dt

                    concluded = await self._conclude_team_meetings(
                        team.id, dt.now(), "stale_team_closed", _repo
                    )
                    logger.info(
                        "StateReaper: team '%s' closed (%d offline, %d meetings)",
                        team.name,
                        len(agents),
                        concluded,
                    )

    async def _workflow_team_runs(
        self, team, cfg: dict, repo: StorageRepository
    ) -> list | None:
        """Every run a workflow team owns, or None when the answer can't be trusted.

        Two links exist and both are followed: ``workflow_runs.team_id`` (written by
        ingest/receipt) and ``team.config.workflow_run_id`` (written when a shell team
        is adopted). The 3 stale teams found on 2026-07-28 only had the second one —
        their runs had since been re-linked to per-run teams — which is exactly why a
        team_id-only lookup would have read them as "no runs at all".

        None means a lookup failed; the caller must then leave the team alone rather
        than mistake a query error for "nothing is running".
        """
        try:
            runs = await repo.list_workflow_runs_by_team(team.id)
        except Exception:  # noqa: BLE001 — 查不到就不收，绝不把查询失败当成"没在跑"
            logger.debug("workflow team runs lookup failed team=%s", team.id, exc_info=True)
            return None
        adopted = str(cfg.get("workflow_run_id") or "")
        if adopted and not any(r.wf_id == adopted for r in runs):
            try:
                run = await repo.get_workflow_run(adopted)
            except Exception:  # noqa: BLE001
                return None
            # 认养 id 指向的 run 行不存在（记录从未落库/已清）→ 当作零 run 证据，
            # 由调用方的长宽限兜住，不当作"跑完了"。
            if run is not None:
                runs.append(run)
        return runs

    async def _close_settled_workflow_team(
        self,
        team,
        cfg: dict,
        now: datetime,
        stale_threshold: datetime,
        repo: StorageRepository,
    ) -> None:
        """Close a workflow team once its runs are over and nobody is working in it.

        The blanket workflow exemption had exactly one way out — an empty
        ``workflow-session-<sid8>`` shell — so a team that kept its members never
        closed at all. 2026-07-28 实测：3 支这样的队挂在 Teams 页上恒 active，最老的
        自 07-06 起 22 天，认养的 run 全部早已终态。豁免本身是对的（长跑期间成员靠
        promote/收尸懒到位，空名册什么都不证明），但它必须随 run 一起结束。

        收队要同时满足三条：
          * 队名下每条 run 都已停（终态，或 interrupted——live tail 判定它不再推进）；
          * 没有成员在 busy（busy 的 leader 行同样算数：那是活会话停在这支队里）；
          * 队龄与成员最后活跃都已越过宽限（run 只提供"停没停"的判据，不提供时钟——
            真正还有活儿的证据是成员活跃，它同时也是 straggler 注册的第一现场）。
        仍有 running/planned run 的队，原样维持整类豁免。
        """
        members = await repo.list_agents(team.id)
        if any(str(getattr(m, "status", "")).endswith("busy") for m in members):
            return

        runs = await self._workflow_team_runs(team, cfg, repo)
        if runs is None:
            return
        if any(str(r.status) not in _WORKFLOW_SETTLED_STATUSES for r in runs):
            return  # 还有 run 在跑/待跑 → 维持豁免，等 promote 收尾与 straggler 注册

        reference = team.created_at
        for m in members:
            # 没有心跳记录的成员用入队时间兜底（同 _check_hook_agent 的基线选择）：
            # 刚注册、还没发出第一次心跳的成员不能被当成"上古静默"。
            ts = getattr(m, "last_active_at", None) or getattr(m, "created_at", None)
            if ts is not None and (reference is None or ts > reference):
                reference = ts
        if reference is None:
            return

        if runs or not members:
            # 有 run 记录（或队里连人都没有）→ 常规 30 分钟陈旧阈值。空壳沿用 30 分钟
            # 是既有行为，2026-07-10 实锤空壳空挂 2h，不回退。
            deadline = stale_threshold
        else:
            # 零 run 记录却有成员：证据链整条缺失，多等 WORKFLOW_TEAM_NO_RUN_GRACE_HOURS
            # （理由写在 settings 常量旁）。
            deadline = now - timedelta(hours=WORKFLOW_TEAM_NO_RUN_GRACE_HOURS)
        if reference >= deadline:
            return

        await repo.update_team(team.id, status="completed", completed_at=now)
        for m in members:
            # 寄生在 workflow 队里的历史 Leader 行不归本队收（03fe7cae 实录，与
            # workflow_ingest 终态收工的排除同源）：它的活性属于会话，不属于这条 run。
            if str(getattr(m, "role", "")) == "leader":
                continue
            if not str(getattr(m, "status", "")).endswith("offline"):
                await repo.update_agent(m.id, status="offline", current_task=None)
        logger.info(
            "StateReaper: closed settled workflow team '%s' (%d run(s), %d member(s))",
            team.name,
            len(runs),
            len(members),
        )

    async def _purge_spent_session_containers(
        self, repo: StorageRepository | None = None
    ) -> None:
        """Sweep up ``session-<sid8>`` container teams nobody will look at again.

        Rides the existing reap tick on purpose: the no-timer rule stands (periodic
        cron was deliberately retired), so housekeeping goes where the loop already
        is. The predicate and the cascade live in the repository — see
        ``purge_stale_session_containers`` for why deletion is so hard to earn.
        """
        _repo = repo if repo is not None else self._repo
        try:
            purged = await _repo.purge_stale_session_containers(
                retention_days=CONTAINER_TEAM_RETENTION_DAYS,
                limit=CONTAINER_TEAM_PURGE_MAX_PER_CYCLE,
            )
        except Exception:
            logger.warning("Session container purge failed", exc_info=True)
            return
        for row in purged:
            await self._event_bus.emit(
                "team.container_purged",
                f"team:{row['team_id']}",
                row,
            )
        if purged:
            logger.info(
                "StateReaper: purged %d spent session container team(s): %s",
                len(purged),
                ", ".join(r["name"] for r in purged),
            )

    async def _check_workflow_ingest(self, repo: StorageRepository | None = None) -> None:
        """I3a: 保底轮询 Workflow 完成检测 + Phase2 live 追踪（同 tick，零新 timer）。

        Cheap-Checks-First：观察集 = running ∪ 24h 复查窗内 interrupted，皆空即整段
        跳过（稳态零文件 stat；interrupted 无变化时 updated_at 冻结，过窗自然老化）。
        非空则：① 对观察集所属项目 reconcile（终态文件先行覆盖，fingerprint 短路）；
        ② 重新取观察集（① 可能已转终态），按 updated_at 最旧优先截断
        WF_LIVE_TAIL_MAX_RUNS 后逐 run live tail（journal 增量 + lastCtx token +
        interrupted 判定）；interrupted run 加调 .output 兜底富化。
        整段仍在治理租约门控 + _reap_cycle 30s 硬超时内，try/except 隔离。
        """
        from aiteam.api import workflow_ingest

        _repo = repo if repo is not None else self._repo

        async def _watch_runs() -> list:
            """running ∪ 24h 复查窗内 interrupted（list 失败按空处理）。"""
            runs = await _repo.list_workflow_runs(status="running", limit=200)
            try:
                interrupted = await _repo.list_workflow_runs(
                    status="interrupted", limit=200
                )
            except Exception:
                interrupted = []
            cutoff = datetime.now() - timedelta(
                hours=workflow_ingest.WF_INTERRUPTED_RECHECK_HOURS
            )
            runs.extend(r for r in interrupted if r.updated_at and r.updated_at >= cutoff)
            return runs

        try:
            watch = await _watch_runs()
        except Exception:
            logger.debug("workflow ingest check: list runs failed", exc_info=True)
            return
        if not watch:
            return  # 稳态：无 running 且无近期 interrupted，不做任何文件 stat

        scan_all = any(not r.project_id for r in watch)
        dirs: set[str] = set()
        for pid in {r.project_id for r in watch if r.project_id}:
            try:
                proj = await _repo.get_project(pid)
            except Exception:
                proj = None
            if proj and proj.root_path:
                dirs.add(proj.root_path)

        try:
            if scan_all or not dirs:
                await workflow_ingest.reconcile(_repo, self._event_bus, project_dir=None)
            else:
                for d in dirs:
                    await workflow_ingest.reconcile(_repo, self._event_bus, project_dir=d)
        except Exception:
            logger.warning("workflow ingest check failed", exc_info=True)

        # Phase2 live tail：重新取观察集（reconcile 可能已把部分转终态），
        # 最旧优先截断，防 _reap_cycle 30s 硬超时。
        try:
            live = await _watch_runs()
        except Exception:
            logger.debug("workflow live tail: relist failed", exc_info=True)
            return
        live.sort(key=lambda r: r.updated_at or r.created_at)
        for r in live[: workflow_ingest.WF_LIVE_TAIL_MAX_RUNS]:
            try:
                res = await workflow_ingest.tail_live_run(_repo, self._event_bus, r)
            except Exception:
                logger.debug("workflow live tail failed wf=%s", r.wf_id, exc_info=True)
                continue
            newly_marked = bool(isinstance(res, dict) and res.get("marked_interrupted"))
            if r.status == "interrupted" or newly_marked:
                try:
                    await workflow_ingest.enrich_from_task_output(_repo, r)
                except Exception:
                    logger.debug(
                        "workflow output enrich failed wf=%s", r.wf_id, exc_info=True
                    )

    async def _agent_session_live(self, agent, repo: StorageRepository) -> bool:
        """True if the agent's owning CC session transcript is fresh (< LIVE window).

        File truth source guard (fleet-layer §5): used to avoid offlining a live
        session's members. Any resolution failure returns False (fall through to the
        caller's normal offline path) so this only ever spares, never over-keeps.

        This is still the **only** verdict that decides anything. The CC session
        registry gives a second, more direct reading (real pid + idle/busy status);
        it runs alongside purely to accumulate comparison data — see
        ``_observe_liveness_tracks``. Switching tracks is a separate, evidence-gated
        decision (C13).
        """
        from aiteam.api import session_probe

        sid = getattr(agent, "session_id", "") or ""
        pid = getattr(agent, "project_id", "") or ""
        if not sid or not pid:
            return False
        verdict = False
        try:
            proj = await repo.get_project(pid)
            root = (proj.root_path or "") if proj else ""
            if root:
                last = session_probe.session_last_active(root, sid)
                if last is not None:
                    verdict = (datetime.now() - last) < timedelta(minutes=15)
        except Exception:  # noqa: BLE001 — probe failure must not keep zombies alive
            verdict = False
        await self._observe_liveness_tracks(agent, sid, verdict)
        return verdict

    async def _observe_liveness_tracks(self, agent, session_id: str, mtime_verdict: bool) -> None:
        """Record where the transcript-mtime track and the CC session registry disagree.

        Parallel observation only (C13): nothing here influences the decision the
        caller just made. The registry reads ``~/.claude/sessions/<pid>.json``, so
        it can tell "process gone" from "process idle and quiet" — a distinction
        mtime freshness cannot make, and the source of both historical failure
        modes (a quiet-but-alive session reaped as dead; a crashed session kept
        alive by a transcript someone else touched).

        A registry answer of None means "this session was never registered"
        (workflow injection, tests, pre-registry sessions) — absence of a record
        is not evidence of death, so it is never counted as a disagreement.
        """
        from aiteam.api import session_registry

        try:
            registry_verdict = session_registry.session_alive(session_id)
            if registry_verdict is None or registry_verdict == mtime_verdict:
                return
            now = datetime.now()
            last = self._liveness_divergence_seen.get(session_id)
            if last is not None and (now - last) < timedelta(minutes=10):
                return
            self._liveness_divergence_seen[session_id] = now
            record = session_registry.find_session(session_id)
            await self._event_bus.emit(
                "session.liveness_divergence",
                f"session:{session_id}",
                {
                    "session_id": session_id,
                    "agent_id": getattr(agent, "id", ""),
                    "agent_name": getattr(agent, "name", ""),
                    "mtime_live": mtime_verdict,
                    "registry_live": registry_verdict,
                    "registry_status": getattr(record, "status", ""),
                    "registry_pid": getattr(record, "pid", 0),
                    "decided_by": "mtime",
                },
            )
            logger.info(
                "Liveness tracks disagree for session %s: mtime=%s registry=%s "
                "(status=%s) — decision still taken from mtime",
                session_id[:8],
                mtime_verdict,
                registry_verdict,
                getattr(record, "status", "?"),
            )
        except Exception:  # noqa: BLE001 — 观察失败绝不能影响存活判定
            logger.debug("liveness track comparison failed", exc_info=True)
    async def _backfill_agent_watermarks(
        self, repo: StorageRepository | None = None
    ) -> None:
        """Backfill sub-agent context watermarks when SubagentStop missed them.

        P1 ledger (batch 1B): the event-driven capture in hook_translator is the
        main path; this is the safety net for rows whose SubagentStop was lost or
        never fired. Cheap-checks-first (matches the reaper's design principle):
        stat the transcript and skip when its mtime has not advanced past
        ctx_measured_at, so no re-read and no DB write happen in steady state.
        Bounded to hook agents with a cc_tool_use_id active within 30 days.
        See docs/agent-reuse-design.md section 4.4.
        """
        _repo = repo if repo is not None else self._repo
        now = datetime.now()
        cutoff = now - timedelta(days=30)
        try:
            teams = await _repo.list_teams()
        except Exception:
            return
        project_roots: dict[str, str] = {}
        for team in teams:
            try:
                agents = await _repo.list_agents(team.id)
            except Exception:
                continue
            for agent in agents:
                cc_id = getattr(agent, "cc_tool_use_id", None)
                if not cc_id:
                    continue
                reference_time = agent.last_active_at or agent.created_at
                if reference_time is None or reference_time < cutoff:
                    continue
                # Resolve the launching project's root (cached per project).
                root = ""
                project_id = getattr(agent, "project_id", None)
                if project_id:
                    if project_id not in project_roots:
                        try:
                            proj = await _repo.get_project(project_id)
                            project_roots[project_id] = getattr(proj, "root_path", "") or ""
                        except Exception:
                            project_roots[project_id] = ""
                    root = project_roots[project_id]
                transcript = agent_context.locate_transcript(
                    stored_path=getattr(agent, "transcript_path", None),
                    cc_tool_use_id=cc_id,
                    session_id=getattr(agent, "session_id", None),
                    project_root=root or None,
                )
                if transcript is None:
                    continue
                # Cheap short-circuit: skip when the transcript has not changed
                # since the last measurement (no re-read, no write).
                try:
                    mtime = datetime.fromtimestamp(transcript.stat().st_mtime)
                except OSError:
                    continue
                measured_at = getattr(agent, "ctx_measured_at", None)
                if measured_at is not None and mtime <= measured_at:
                    continue
                measured = agent_context.measure(transcript)
                if measured is None:
                    continue
                measured["transcript_path"] = str(transcript)
                try:
                    await _repo.update_agent(agent.id, **measured)
                except Exception:
                    continue

    async def _check_agent_liveness(self, repo: StorageRepository | None = None) -> None:
        """Detect agent liveness based on CC team config."""
        import json as _json
        from pathlib import Path

        _repo = repo if repo is not None else self._repo

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
        teams = await _repo.list_teams()
        for team in teams:
            if team.status != "active":
                continue
            agents = await _repo.list_agents(team.id)
            for agent in agents:
                if agent.source != "hook" or agent.status == "offline":
                    continue
                # Leader 由 SessionStart/SessionEnd + 工具事件活性触摸管理，不在
                # ~/.claude/teams 配置里——按成员名探活必然失败。旧代码只按名字
                # 豁免 "team-lead"，而实际行名是 "Leader"（2026-07-07 实测：每个
                # tick 被收割一次，刚复活 60s 内又被打 offline）。按角色豁免。
                if agent.role == "leader" or agent.name == "team-lead":
                    continue
                # workflow 子 agent 不在 ~/.claude/teams 配置里（它们是 CC Workflow
                # 内部 fan-out，不是 Agent Teams 成员）——按成员名探活必然失败，曾令
                # 活 agent 每个 tick 被打 offline（2026-07-06 监控实录：62~189s 即
                # 误杀且单向不恢复）。其生死由观测层 live tail / SubagentStop /
                # 加长心跳兜底（见 _check_hook_agent），此处豁免。
                # 角色常量与 hook_translator.WORKFLOW_AGENT_TYPE 保持一致。
                if agent.role == "workflow-subagent":
                    continue
                # busy/waiting agent not in any team config -> offline, UNLESS its
                # owning CC session is still live by file mtime (fleet-layer §5:
                # don't offline a live session's Agent-tool members just because they
                # aren't ~/.claude/teams members — session file mtime is authoritative).
                if agent.name not in alive_names:
                    if await self._agent_session_live(agent, _repo):
                        continue
                    await _repo.update_agent(
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

    async def _conclude_team_meetings(
        self,
        team_id: str,
        now: datetime,
        trigger: str,
        repo: StorageRepository | None = None,
    ) -> int:
        """Conclude all active meetings for a team. Returns count of concluded meetings."""
        _repo = repo if repo is not None else self._repo

        meetings = await _repo.list_meetings(team_id, status=MeetingStatus.ACTIVE)
        count = 0
        for meeting in meetings:
            await _repo.update_meeting(
                meeting.id, status=MeetingStatus.CONCLUDED.value, concluded_at=now
            )
            await self._event_bus.emit(
                "meeting.concluded",
                f"meeting:{meeting.id}",
                {"meeting_id": meeting.id, "topic": meeting.topic, "team_id": team_id, "trigger": trigger},
            )
            count += 1
        if count:
            logger.info("Auto-concluded %d meeting(s) for team %s (trigger=%s)", count, team_id, trigger)
        return count

    async def _check_meeting_expiry(
        self, now: datetime, repo: StorageRepository | None = None
    ) -> None:
        """Check and auto-conclude expired meetings.

        Active meetings with no new messages for MEETING_EXPIRY_MINUTES are auto-concluded.
        """
        _repo = repo if repo is not None else self._repo

        expiry_threshold = now - timedelta(minutes=MEETING_EXPIRY_MINUTES)
        teams = await _repo.list_teams()

        for team in teams:
            meetings = await _repo.list_meetings(
                team.id,
                status=MeetingStatus.ACTIVE,
            )
            for meeting in meetings:
                # Get meeting messages, take the latest one's timestamp
                # list_meeting_messages sorts by timestamp ASC, take the last one
                messages = await _repo.list_meeting_messages(
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
                    await _repo.update_meeting(
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

    async def _check_scheduled_tasks(
        self, now: datetime, repo: StorageRepository | None = None
    ) -> None:
        """Execute due scheduled tasks.

        For each due task:
        - If past-due > 1 hour: skip (treat as missed, don't pile up)
        - Execute action based on action_type
        - Update last_run_at and next_run_at regardless of action success
        - Each task has independent try/except so one failure won't block others
        """
        _repo = repo if repo is not None else self._repo
        due_tasks = await _repo.get_due_tasks(now)
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
                    await _repo.update_scheduled_task(
                        sched_task.id,
                        last_run_at=now,
                        next_run_at=next_run,
                    )
                    continue

                await self._execute_scheduled_action(sched_task, now, _repo)

                next_run = now + timedelta(seconds=sched_task.interval_seconds)
                await _repo.update_scheduled_task(
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

    async def _execute_scheduled_action(
        self,
        sched_task,
        now: datetime,
        repo: StorageRepository | None = None,
    ) -> None:
        """Dispatch a scheduled task's action.

        Only ``wake_agent`` remains — create_task / inject_reminder / emit_event were
        isomorphic to CC's native Cron* tools and were retired 2026-07-27 (batch 6).
        """
        action = sched_task.action_type

        if action == "wake_agent":
            try:
                result = await self._wake_manager.try_wake(sched_task)
                logger.info("wake_agent: %s → %s", sched_task.name, result)
            except Exception as e:
                logger.error("wake_agent: %s failed: %s", sched_task.name, e, exc_info=True)

        else:
            logger.warning(
                "Unknown scheduled action type '%s' for task '%s'",
                action,
                sched_task.name,
            )
