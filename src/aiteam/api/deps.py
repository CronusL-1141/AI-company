"""AI Team OS — API dependency injection.

Provides TeamManager singleton and StorageRepository lifespan management.
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import Request
from sqlalchemy import inspect, text

from aiteam.api.event_bus import EventBus
from aiteam.api.hook_translator import HookTranslator
from aiteam.api.state_reaper import StateReaper
from aiteam.loop.engine import LoopEngine
from aiteam.loop.watchdog import WatchdogChecker, WatchdogRunner
from aiteam.memory.store import MemoryStore
from aiteam.orchestrator.team_manager import TeamManager
from aiteam.storage.connection import close_db, get_engine
from aiteam.storage.repository import StorageRepository
from aiteam.types import AgentStatus

logger = logging.getLogger(__name__)

# Module-level singletons
_repository: StorageRepository | None = None
_memory_store: MemoryStore | None = None
_event_bus: EventBus | None = None
_manager: TeamManager | None = None
_reaper: StateReaper | None = None
_watchdog_runner: WatchdogRunner | None = None
_hook_translator: HookTranslator | None = None
_loop_engine: LoopEngine | None = None


def _run_alembic_stamp_head(db_url: str) -> None:
    """Stamp the existing database as being at the latest Alembic revision.

    Used for databases that were created/migrated via the legacy _run_migrations()
    approach. Tells Alembic "this DB is already up to date" so future incremental
    migrations can be tracked properly.
    """
    try:
        from alembic import command
        from alembic.config import Config

        # Locate alembic.ini relative to this package (project root)
        ini_path = Path(__file__).parents[4] / "alembic.ini"
        if not ini_path.exists():
            logger.warning("alembic.ini not found at %s, skipping stamp", ini_path)
            return

        alembic_cfg = Config(str(ini_path))
        alembic_cfg.set_main_option("sqlalchemy.url", db_url)
        command.stamp(alembic_cfg, "head")
        logger.info("Alembic: stamped existing DB as head")
    except Exception as exc:
        logger.warning("Alembic stamp failed (non-fatal): %s", exc)


def _get_alembic_current_revision(db_url: str) -> str | None:
    """Return the current Alembic revision for the given DB, or None if untracked."""
    try:
        from alembic.migration import MigrationContext
        from sqlalchemy import create_engine

        # Use sync engine for Alembic inspection (aiosqlite URL -> sqlite URL)
        sync_url = db_url.replace("sqlite+aiosqlite", "sqlite")
        engine = create_engine(sync_url, connect_args={"check_same_thread": False})
        with engine.connect() as conn:
            ctx = MigrationContext.configure(conn)
            rev = ctx.get_current_revision()
        engine.dispose()
        return rev
    except Exception:
        return None


async def _run_migrations(db_url: str | None = None) -> None:
    """Run database migrations — add new columns to existing tables.

    SQLAlchemy create_all only creates non-existing tables, it won't ALTER existing ones.
    This function checks and adds columns introduced in M3.1.
    """
    engine = get_engine(db_url)

    # Columns to add: (table_name, column_name, column_type_sql)
    migrations: list[tuple[str, str, str]] = [
        ("teams", "project_id", "VARCHAR(36)"),
        ("teams", "status", "VARCHAR(20) DEFAULT 'active'"),
        ("agents", "project_id", "VARCHAR(36)"),
        ("agents", "current_phase_id", "VARCHAR(36)"),
        ("tasks", "project_id", "VARCHAR(36)"),
        ("tasks", "parent_id", "VARCHAR(36)"),
        ("tasks", "depends_on", "JSON DEFAULT '[]'"),
        ("tasks", "depth", "INTEGER DEFAULT 0"),
        ("tasks", "order", "INTEGER DEFAULT 0"),
        ("tasks", "template_id", "VARCHAR(50)"),
        ("meetings", "project_id", "VARCHAR(36)"),
        ("tasks", "config", "JSON DEFAULT '{}'"),
        # v0.9.0: event log enhancement
        ("events", "entity_id", "VARCHAR(36)"),
        ("events", "entity_type", "VARCHAR(50)"),
        ("events", "state_snapshot", "JSON"),
        # v1.0 P2: agent trust scoring
        ("agents", "trust_score", "REAL DEFAULT 0.5"),
        # Stage B: ecosystem_repo_profiles 扩展字段
        ("ecosystem_repo_profiles", "pushed_at", "DATETIME"),
        ("ecosystem_repo_profiles", "is_archived", "BOOLEAN DEFAULT 0"),
        ("ecosystem_repo_profiles", "scan_run_id", "VARCHAR(36)"),
        ("ecosystem_repo_profiles", "description_excerpt", "TEXT DEFAULT ''"),
    ]
    # v1.0 P1-6: channel_messages table (created via create_all, no ALTER needed)
    # Stage B: ecosystem_deep_reviews / ecosystem_tags / ecosystem_repo_tags /
    # ecosystem_relations / ecosystem_scan_runs 由 create_all 自动建表

    async with engine.connect() as conn:
        for table_name, col_name, col_type in migrations:
            # Check if column already exists
            has_column = await conn.run_sync(
                lambda sync_conn, t=table_name, c=col_name: (
                    c in [col["name"] for col in inspect(sync_conn).get_columns(t)]
                    if inspect(sync_conn).has_table(t)
                    else False
                )
            )
            if not has_column:
                # SAFETY: values are hardcoded constants, not user input
                await conn.execute(
                    text(f"ALTER TABLE {table_name} ADD COLUMN {col_name} {col_type}")
                )
                logger.info("Migration: added column %s to table %s", col_name, table_name)

        # Value migration: idle -> waiting (three-state model upgrade)
        await conn.execute(text("UPDATE agents SET status='waiting' WHERE status='idle'"))

        # B1: ensure task status index exists on legacy DBs
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_tasks_status ON tasks (status)")
        )

        # Migration: tasks.team_id from NOT NULL to nullable (support project-level tasks)
        team_id_nullable = await conn.run_sync(
            lambda sync_conn: (
                next(
                    (
                        col["nullable"]
                        for col in inspect(sync_conn).get_columns("tasks")
                        if col["name"] == "team_id"
                    ),
                    True,  # If column not found, skip
                )
                if inspect(sync_conn).has_table("tasks")
                else True
            )
        )
        if not team_id_nullable:
            logger.info(
                "Migration: rebuilding tasks table with nullable team_id (project-level tasks)"
            )
            await conn.execute(text("CREATE TABLE tasks_new AS SELECT * FROM tasks"))
            await conn.execute(text("DROP TABLE tasks"))
            await conn.execute(
                text("""
                CREATE TABLE tasks (
                    id VARCHAR(36) PRIMARY KEY,
                    team_id VARCHAR(36),
                    title VARCHAR(500) NOT NULL,
                    description TEXT DEFAULT '',
                    status VARCHAR(20) DEFAULT 'pending',
                    assigned_to VARCHAR(36),
                    result TEXT,
                    parent_id VARCHAR(36),
                    project_id VARCHAR(36),
                    depends_on JSON DEFAULT '[]',
                    depth INTEGER DEFAULT 0,
                    "order" INTEGER DEFAULT 0,
                    template_id VARCHAR(50),
                    priority VARCHAR(20) DEFAULT 'medium',
                    horizon VARCHAR(20) DEFAULT 'short',
                    tags JSON DEFAULT '[]',
                    config JSON DEFAULT '{}',
                    created_at DATETIME,
                    started_at DATETIME,
                    completed_at DATETIME
                )
            """)
            )
            await conn.execute(text("INSERT INTO tasks SELECT * FROM tasks_new"))
            await conn.execute(text("DROP TABLE tasks_new"))
            await conn.execute(text("CREATE INDEX ix_tasks_team_id ON tasks (team_id)"))
            logger.info("Migration: tasks table rebuild complete")

        await conn.commit()


_DEFAULT_ECOSYSTEM_TAGS: list[tuple[str, str, str]] = [
    # capability
    ("memory_system", "capability", "记忆 / 长期上下文系统"),
    ("skill_system", "capability", "Skill / 技能注入系统"),
    ("agent_orchestration", "capability", "多 Agent 编排"),
    ("mcp_server", "capability", "MCP Server 实现"),
    ("mcp_framework", "capability", "MCP 框架 / SDK (用于构建 MCP)"),
    ("tool_use", "capability", "工具调用相关"),
    ("workflow_engine", "capability", "工作流引擎"),
    ("multi_agent", "capability", "多 Agent 协作系统"),
    ("single_agent", "capability", "单 Agent 系统"),
    ("claude_code", "capability", "Claude Code 生态 (skills/hooks/agents)"),
    ("agent_harness", "capability", "Agent 运行时 / 框架外壳"),
    # tech_stack
    ("python", "tech_stack", "Python 实现"),
    ("typescript", "tech_stack", "TypeScript 实现"),
    ("javascript", "tech_stack", "JavaScript 实现"),
    ("java", "tech_stack", "Java 实现"),
    ("rust", "tech_stack", "Rust 实现"),
    ("go", "tech_stack", "Go 实现"),
    # maturity
    ("official_anthropic", "maturity", "Anthropic 官方项目"),
    ("battle_tested", "maturity", "已经过实战检验"),
    ("experimental", "maturity", "实验性 / 尝鲜阶段"),
    # positioning
    ("framework", "positioning", "框架级项目"),
    ("application", "positioning", "终端应用"),
    ("library", "positioning", "工具库"),
    ("plugin", "positioning", "插件 / 扩展"),
    ("template", "positioning", "项目模板"),
    ("docs_only", "positioning", "纯文档 / 教程 / awesome-list / cookbook"),
    # v1.5.0-A: lifecycle (positioning) — 漏斗 Stage 3 + 失败状态自动打
    ("evaluating", "positioning", "正在评估中（Stage 1-2）"),
    ("reference", "positioning", "已作为架构借鉴参考"),
    ("integrated", "positioning", "已集成到本项目"),
    ("deleted", "positioning", "GitHub 端仓已被删除"),
    ("private_now", "positioning", "GitHub 端仓已设为私密"),
]


async def ensure_default_tags(repo: StorageRepository) -> None:
    """启动时确保默认 31 个 EcosystemTag 存在 (INSERT OR IGNORE 语义)。

    v1.5.0-A: 26 → 31 (新增 5 个 lifecycle positioning tags：
    evaluating / reference / integrated / deleted / private_now)。

    使用 upsert_tag 实现幂等：已存在则保留，不会覆盖人工修改的描述。
    实际逻辑通过 get_tag_by_name 检查后才插入，避免覆盖。
    """
    from aiteam.types import EcosystemTag, EcosystemTagCategory

    inserted = 0
    for name, category, description in _DEFAULT_ECOSYSTEM_TAGS:
        existing = await repo.get_tag_by_name(name)
        if existing is not None:
            continue
        await repo.upsert_tag(
            EcosystemTag(
                name=name,
                category=EcosystemTagCategory(category),
                description=description,
            )
        )
        inserted += 1
    if inserted > 0:
        logger.info("Ensured %d default ecosystem tags inserted", inserted)


async def _backfill_ecosystem_default_project(repo: StorageRepository) -> None:
    """Stage J 启动迁移：把历史 project_id IS NULL 的 ecosystem 行
    回填到当前 cwd 匹配的项目，保证旧 188 仓在 "AI Team OS" 项目下仍可见。

    选择规则：
    1. 优先匹配 cwd 最长前缀的项目（与 _auto_create_projects 一致）；
    2. 找不到时回退到首个项目；
    3. 没有任何项目时跳过（数据保持 NULL，待项目创建后再次 backfill）。

    幂等：仅迁移 NULL 行，已有 project_id 的行不动。
    """
    import os

    try:
        projects = await repo.list_projects()
    except Exception as exc:
        logger.warning("ecosystem backfill: list_projects failed: %s", exc)
        return
    if not projects:
        logger.info("ecosystem backfill: no projects yet, skipping")
        return

    cwd = os.getcwd().replace("\\", "/").rstrip("/").lower()
    target = None
    best_len = -1
    for p in projects:
        rp = (p.root_path or "").replace("\\", "/").rstrip("/").lower()
        if rp and (cwd == rp or cwd.startswith(rp + "/")) and len(rp) > best_len:
            target = p
            best_len = len(rp)
    if target is None:
        target = projects[0]

    try:
        counts = await repo.backfill_ecosystem_to_project(target.id)
    except Exception as exc:
        logger.warning("ecosystem backfill failed: %s", exc)
        return

    total = sum(counts.values())
    if total > 0:
        logger.info(
            "Ecosystem backfill: migrated %d rows to project %s (%s)",
            total,
            target.name,
            target.id[:8],
        )
        for k, v in counts.items():
            if v > 0:
                logger.info("  %s: %d", k, v)
    else:
        logger.info("Ecosystem backfill: no NULL rows to migrate")


async def _backfill_v150_progressive_funnel(repo: StorageRepository) -> None:
    """v1.5.0-A 启动迁移：

    1) 现有 DeepReview.stage_status 推断
       - 已完成且有 risks_md+learnings_md (说明跑过深扫总结) → DEBATED
       - 否则有 architecture_md → ARCHITECTURE_DONE
       - 否则有 summary_md → SHALLOW_DONE
       - 其他保持 QUEUED
    2) 现有 Profile.is_active 字段（默认 True 已由 column default 保证）
    3) 为每个项目创建 EcosystemProjectSettings 默认行
       - 名称含 "AI Team OS" 的项目用严格默认 (min_stars=5000, top_n=200)
       - 其他项目用通用默认 (min_stars=1000, top_n=100)

    幂等：每次启动重跑都安全，已有 stage_status / settings 不动。
    """
    try:
        projects = await repo.list_projects()
    except Exception as exc:
        logger.warning("v1.5.0 backfill: list_projects failed: %s", exc)
        return

    # 1) DeepReview stage_status 推断（仅迁移 stage_status='queued' 的历史行）
    try:
        deep_reviews = await repo.list_deep_reviews(limit=10000, project_id=None)
    except Exception as exc:
        logger.warning("v1.5.0 backfill: list_deep_reviews failed: %s", exc)
        deep_reviews = []

    promoted_counts = {
        "shallow_done": 0,
        "architecture_done": 0,
        "debated": 0,
        "kept_queued": 0,
    }
    from aiteam.types import EcosystemDeepReviewStatus, EcosystemStageStatus

    for review in deep_reviews:
        # 仅迁移 stage_status 仍是默认 QUEUED 的行；保护已手工推进的状态
        if review.stage_status != EcosystemStageStatus.QUEUED:
            continue
        # 推断规则（保守，从严到松）
        if (
            review.status == EcosystemDeepReviewStatus.COMPLETED
            and review.risks_md
            and review.learnings_md
        ):
            new_stage = EcosystemStageStatus.DEBATED
            promoted_counts["debated"] += 1
        elif review.architecture_md:
            new_stage = EcosystemStageStatus.ARCHITECTURE_DONE
            promoted_counts["architecture_done"] += 1
        elif review.summary_md:
            new_stage = EcosystemStageStatus.SHALLOW_DONE
            promoted_counts["shallow_done"] += 1
        else:
            promoted_counts["kept_queued"] += 1
            continue
        try:
            await repo.update_deep_review_stage(
                review.id,
                new_stage,
                project_id=review.project_id,
            )
        except Exception as exc:
            logger.warning(
                "v1.5.0 backfill: update_deep_review_stage(%s) failed: %s",
                review.id[:8],
                exc,
            )

    if any(v > 0 for v in promoted_counts.values()):
        logger.info(
            "v1.5.0 backfill: deep_review stage_status promoted "
            "(shallow_done=%d, architecture_done=%d, debated=%d, kept_queued=%d)",
            promoted_counts["shallow_done"],
            promoted_counts["architecture_done"],
            promoted_counts["debated"],
            promoted_counts["kept_queued"],
        )

    # 2) 项目 ecosystem_project_settings 默认值
    settings_created = 0
    for p in projects:
        is_ai_team_os = "ai team os" in (p.name or "").lower() or "ai-team-os" in (
            p.name or ""
        ).lower()
        try:
            existing = await repo.get_ecosystem_project_settings(p.id)
            if existing is None:
                await repo.ensure_ecosystem_project_settings(
                    p.id, is_ai_team_os=is_ai_team_os
                )
                settings_created += 1
        except Exception as exc:
            logger.warning(
                "v1.5.0 backfill: ensure_ecosystem_project_settings(%s) failed: %s",
                p.name,
                exc,
            )

    if settings_created > 0:
        logger.info(
            "v1.5.0 backfill: created %d EcosystemProjectSettings rows",
            settings_created,
        )


async def _auto_create_projects(repo: StorageRepository) -> None:
    """Auto-create Projects for Teams without project_id and link them."""
    teams = await repo.list_teams()
    orphan_teams = [t for t in teams if not t.project_id]
    # workflow 队不参与 cwd 兜底认领：其归属由观测层按落盘 slug（文件真相源）
    # 精确回填（用户 2026-07-07 定案：曾有外部项目的 workflow 队被 API 进程
    # cwd 吸进 OS 项目——"收纳进首个项目"对 workflow 队废止；匹配不到项目
    # 就保持无主，绝不猜）。
    orphan_teams = [
        t for t in orphan_teams if (t.config or {}).get("kind") != "workflow"
    ]
    if not orphan_teams:
        return
    # Check if existing projects can be reused
    existing_projects = await repo.list_projects()
    if existing_projects:
        # Assign all orphan Teams to the best-matching Project by cwd
        import os
        cwd = os.getcwd().replace("\\", "/").rstrip("/").lower()
        # Longest-prefix match — multiple projects can match via prefix
        # (e.g. C:/Users/TUF and C:/Users/TUF/Desktop/AI...); pick the most specific.
        project = None
        best_len = -1
        for p in existing_projects:
            rp = (p.root_path or "").replace("\\", "/").rstrip("/").lower()
            if rp and (cwd == rp or cwd.startswith(rp + "/")) and len(rp) > best_len:
                project = p
                best_len = len(rp)
        if not project:
            project = existing_projects[0]  # ultimate fallback
        for team in orphan_teams:
            await repo.update_team(team.id, project_id=project.id)
        logger.info(
            "Linked %d orphan Teams to existing Project: %s", len(orphan_teams), project.name
        )
    else:
        # Create a unified Project, using team_id as unique root_path
        project = await repo.create_project(
            name="AI Team OS",
            root_path=f"auto-{orphan_teams[0].id}",
            description="Auto-created project",
        )
        for team in orphan_teams:
            await repo.update_team(team.id, project_id=project.id)
        logger.info("Auto-created Project and linked %d Teams", len(orphan_teams))


async def _backfill_shallow_done_status(repo: StorageRepository) -> None:
    """v1.6.1 backfill: stage='shallow_done' 行的 status 字段对齐 'completed'。

    幂等：只修改 status='running' 的脏数据行，已是 'completed' 的行不动。
    """
    try:
        count = await repo.backfill_shallow_done_status_completed()
        if count > 0:
            logger.info(
                "v1.6.1 backfill: fixed %d shallow_done rows with status=running -> completed",
                count,
            )
        else:
            logger.debug("v1.6.1 backfill: no shallow_done/running rows to fix")
    except Exception as exc:
        logger.warning("_backfill_shallow_done_status failed (non-fatal): %s", exc)


async def _backfill_task_memos(repo: StorageRepository) -> None:
    """记忆系统 v2 P0：tasks.config['memo'] → task_memos 表一次性回填。

    幂等：task_memos 表非空即视为已迁移，直接跳过；原 JSON 保留不动（档案）。非致命。
    """
    try:
        count = await repo.backfill_task_memos_from_config()
        if count > 0:
            logger.info(
                "memory-v2 P0 backfill: migrated %d legacy memos into task_memos", count
            )
        else:
            logger.debug(
                "memory-v2 P0 backfill: task_memos already populated or no legacy memos"
            )
    except Exception as exc:
        logger.warning("_backfill_task_memos failed (non-fatal): %s", exc)


async def _backfill_dual_axis_convergence(repo: StorageRepository) -> None:
    """D5 双轴收敛回填：status 列全量对齐派生语义（stage_status 为唯一权威轴）。

    5 条幂等 UPDATE 按 R1→R2→F1→F2→F3 固定序执行（反向先于正向铁律），
    详见 repository.backfill_deep_review_dual_axis。必须在
    _backfill_v150_progressive_funnel（按内容标记推 stage）与
    _backfill_shallow_done_status（v1.6.1 子集）之后运行。非致命。
    """
    try:
        counts = await repo.backfill_deep_review_dual_axis()
        total = sum(counts.values())
        if total > 0:
            logger.info(
                "D5 dual-axis backfill: %d rows updated (%s)",
                total,
                ", ".join(f"{k}={v}" for k, v in counts.items()),
            )
        else:
            logger.debug("D5 dual-axis backfill: no rows to fix")
    except Exception as exc:
        logger.warning("_backfill_dual_axis_convergence failed (non-fatal): %s", exc)


async def _backfill_alert_max_new_per_scan(repo: StorageRepository) -> None:
    """v1.6.1 Phase 2 启动迁移：从 scan_profile.alert_thresholds 迁移 max_new_per_scan 到 settings。

    仅迁移 settings.alert_max_new_per_scan 仍是默认值 50 且 scan_profile 有非默认值时。
    幂等：已迁移过的项目跳过（settings 中值 != 50 则视为已迁移或用户已手动设置）。
    """
    try:
        projects = await repo.list_projects()
        if not projects:
            return
        for project in projects:
            try:
                settings = await repo.ensure_ecosystem_project_settings(project.id)
                # 若已非默认值，说明用户已设置或已迁移过，跳过
                current_val = getattr(settings, "alert_max_new_per_scan", 50)
                if current_val != 50:
                    continue
                # 查找该项目活跃的 scan_profile
                scan_profile = await repo.get_active_scan_profile(project.id)
                if scan_profile is None:
                    continue
                profile_data = scan_profile.profile or {}
                alert_thresholds = profile_data.get("alert_thresholds", {})
                sp_max_new = alert_thresholds.get("max_new_per_scan")
                if sp_max_new is not None and sp_max_new != 50:
                    # 迁移非默认值到 settings
                    from aiteam.types import EcosystemProjectSettings as _Settings
                    updated = _Settings(
                        project_id=project.id,
                        min_stars=settings.min_stars,
                        top_n=settings.top_n,
                        refresh_interval_days=settings.refresh_interval_days,
                        auto_shallow_on_archive=settings.auto_shallow_on_archive,
                        focus_topics=settings.focus_topics,
                        focus_languages=settings.focus_languages,
                        shallow_concurrency=settings.shallow_concurrency,
                        deep_concurrency=settings.deep_concurrency,
                        alert_max_new_per_scan=sp_max_new,
                        created_at=settings.created_at,
                    )
                    await repo.upsert_ecosystem_project_settings(updated)
                    logger.info(
                        "backfill alert_max_new_per_scan: project=%s migrated %d from scan_profile",
                        project.id[:8],
                        sp_max_new,
                    )
            except Exception as exc:
                logger.warning(
                    "_backfill_alert_max_new_per_scan project=%s failed: %s",
                    project.id[:8],
                    exc,
                )
    except Exception as exc:
        logger.warning("_backfill_alert_max_new_per_scan failed (non-fatal): %s", exc)


async def _startup_reconciliation(repo: StorageRepository) -> None:
    """Startup reconciliation — reset all BUSY agents to IDLE and clear session associations on OS restart.

    Rationale: OS restart means previous CC sessions no longer exist,
    so all lingering BUSY statuses and session_ids are zombies that need to be cleared.
    Also sets waiting agents with >1 hour of inactivity to offline.
    """
    from datetime import datetime, timedelta

    stale_cutoff = datetime.now() - timedelta(hours=1)
    teams = await repo.list_teams()
    reconciled = 0
    stale_count = 0
    for team in teams:
        agents = await repo.list_agents(team.id)
        for agent in agents:
            needs_update = False
            updates: dict = {}
            if agent.status == AgentStatus.BUSY:
                updates["status"] = AgentStatus.WAITING.value
                updates["current_task"] = None
                needs_update = True
            if agent.session_id:
                updates["session_id"] = None
                needs_update = True
            if needs_update:
                await repo.update_agent(agent.id, **updates)
                reconciled += 1

            # Clean up stale agents: waiting with >1 hour inactivity -> offline
            effective_status = updates.get("status", agent.status)
            if (
                effective_status in (AgentStatus.WAITING, AgentStatus.WAITING.value)
                and agent.last_active_at
                and agent.last_active_at < stale_cutoff
            ):
                await repo.update_agent(agent.id, status=AgentStatus.OFFLINE.value)
                stale_count += 1

    if reconciled > 0:
        logger.warning(
            "Startup reconciliation: %d agents reset (status + session cleared)", reconciled
        )
    else:
        logger.info("Startup reconciliation: no reset needed")
    if stale_count > 0:
        logger.info("Startup reconciliation: %d stale waiting agents set to offline", stale_count)


async def init_dependencies() -> None:
    """Initialize all dependencies (called during lifespan startup)."""
    global _repository, _memory_store, _event_bus, _manager, _reaper, _watchdog_runner, _hook_translator, _loop_engine  # noqa: PLW0603

    _repository = StorageRepository()
    await _repository.init_db()

    # Determine actual DB URL for Alembic operations
    from aiteam.storage.connection import DEFAULT_DB_URL
    _db_url = _repository._db_url or DEFAULT_DB_URL

    # Always run hand-written migrations first — they are idempotent (check column existence
    # before ALTER TABLE) and cover fields added in v1.0/v1.1 that Alembic stamp head may have
    # skipped. Only stamp head when the DB has never been tracked by Alembic.
    await _run_migrations()

    alembic_revision = _get_alembic_current_revision(_db_url)
    if alembic_revision is None:
        # Legacy DB (no alembic_version table): stamp head so future Alembic revisions can run
        logger.info("Alembic: untracked DB detected, stamping head after legacy migrations")
        _run_alembic_stamp_head(_db_url)
    else:
        logger.info("Alembic: DB is at revision %s", alembic_revision)

    _memory_store = MemoryStore(repository=_repository)
    _event_bus = EventBus(repo=_repository)
    _manager = TeamManager(
        repository=_repository,
        memory=_memory_store,
        event_bus=_event_bus,
    )
    _hook_translator = HookTranslator(repo=_repository, event_bus=_event_bus)
    _loop_engine = LoopEngine(repo=_repository)

    # Startup reconciliation: clear lingering BUSY states
    await _startup_reconciliation(_repository)

    # Auto-create Projects for Teams without project_id
    await _auto_create_projects(_repository)

    # Ensure default ecosystem tags exist
    await ensure_default_tags(_repository)

    # Stage J: backfill legacy ecosystem rows to the default project
    # so the existing 188 repos remain visible from "AI Team OS" project.
    await _backfill_ecosystem_default_project(_repository)

    # v1.5.0-A backfill: deep_review stage_status + project ecosystem settings
    await _backfill_v150_progressive_funnel(_repository)

    # Stage J: refresh project_dir → project_id resolution cache
    await refresh_project_dir_cache(_repository)

    # v1.6.1 Phase 2: backfill alert_max_new_per_scan from scan_profile to settings
    await _backfill_alert_max_new_per_scan(_repository)

    # 2026-07-10 用户裁定：ecosystem 周期刷新 cron 退役（CC 非常驻，长期定时器
    # 无意义且其 emit_event 从无消费者）——改为按需 POST /api/ecosystem/refresh
    # + MCP ecosystem_refresh，需要更新时手动调用。_ensure_ecosystem_weekly_cron 已删。

    # Bug 2 二次修复：backfill stage='shallow_done' 行的 status 字段 running -> completed
    await _backfill_shallow_done_status(_repository)

    # D5 双轴收敛：status 派生视图全量回填（R1→R2→F1→F2→F3，幂等）
    await _backfill_dual_axis_convergence(_repository)

    # 记忆系统 v2 P0：task memo 升表一次性回填（表空才跑，幂等）
    await _backfill_task_memos(_repository)

    # Start StateReaper background harvester
    _reaper = StateReaper(repo=_repository, event_bus=_event_bus)
    _reaper.start()

    # Start WatchdogRunner background patrol
    _watchdog_checker = WatchdogChecker(repo=_repository)
    _watchdog_runner = WatchdogRunner(checker=_watchdog_checker, event_bus=_event_bus)
    _watchdog_runner.start()


async def cleanup_dependencies() -> None:
    """Clean up all dependencies (called during lifespan shutdown)."""
    global _repository, _memory_store, _event_bus, _manager, _reaper, _watchdog_runner, _hook_translator, _loop_engine  # noqa: PLW0603

    # Stop WatchdogRunner first
    if _watchdog_runner is not None:
        await _watchdog_runner.stop()
        _watchdog_runner = None

    # Stop StateReaper
    if _reaper is not None:
        await _reaper.stop()
        _reaper = None

    await close_db()
    _repository = None
    _memory_store = None
    _event_bus = None
    _manager = None
    _hook_translator = None
    _loop_engine = None


def get_manager() -> TeamManager:
    """Get TeamManager instance, injected via FastAPI Depends()."""
    if _manager is None:
        msg = "TeamManager not initialized, ensure application has started"
        raise RuntimeError(msg)
    return _manager


def get_repository() -> StorageRepository:
    """Get the default StorageRepository instance."""
    if _repository is None:
        msg = "StorageRepository not initialized"
        raise RuntimeError(msg)
    return _repository


def get_global_repository() -> StorageRepository:
    """Get the default global StorageRepository — always the default DB regardless of project context.

    Used by cross-project message endpoints which must read/write the shared global DB,
    not per-project databases.
    """
    if _repository is None:
        msg = "StorageRepository not initialized"
        raise RuntimeError(msg)
    return _repository


def get_scoped_repository(request: Request) -> StorageRepository:
    """Get a project-scoped StorageRepository.

    Resolution order:
    1. X-Project-Id header (explicit project id, used by Dashboard / MCP).
    2. X-Project-Dir header (path string, resolved to id via root_path
       longest-prefix match). Used by MCP tools that only know the cwd.
    3. Falls back to the global unscoped repository when neither is present.

    When a project scope is resolved, all list/create operations on
    project-aware tables (ecosystem 5 数据表 + tasks/teams/agents) are
    automatically filtered to that project via _apply_project_filter.
    """
    # Delegate to get_repository() so FastAPI dependency overrides on
    # get_repository (used by integration tests) still work.
    base_repo = get_repository()
    project_id = request.headers.get("X-Project-Id", "")
    if not project_id:
        project_dir = request.headers.get("X-Project-Dir", "")
        if project_dir:
            # Decode percent-encoding: MCP side encodes non-ASCII (e.g. Chinese
            # path components) so the header stays latin-1 safe.
            import urllib.parse as _up
            project_dir = _up.unquote(project_dir)
            project_id = _resolve_project_id_from_dir(project_dir)
    if not project_id:
        return base_repo
    return StorageRepository(db_url=base_repo._db_url, project_scope=project_id)


def _resolve_project_id_from_dir(project_dir: str) -> str:
    """Resolve a project_id from a directory path via longest-prefix root_path match.

    Returns "" when no project matches (caller falls back to unscoped repo).
    Cached projects list is fetched lazily; tolerable cost since this only
    runs when the caller did not already supply X-Project-Id.
    """
    if _repository is None or not project_dir:
        return ""
    try:
        cwd = project_dir.replace("\\", "/").rstrip("/").lower()
        # Best-effort sync read by leveraging asyncio loop.run_until_complete is
        # unsafe inside FastAPI request context — instead we keep a tiny in-process
        # cache populated by the lifespan startup hook.
        cache = _project_dir_cache
        if not cache:
            return ""
        best_id = ""
        best_len = -1
        for rp_lower, pid in cache.items():
            if rp_lower and (cwd == rp_lower or cwd.startswith(rp_lower + "/")) and len(rp_lower) > best_len:
                best_id = pid
                best_len = len(rp_lower)
        return best_id
    except Exception:
        return ""


# Module-level cache: lower-cased root_path -> project_id, refreshed at startup.
_project_dir_cache: dict[str, str] = {}


async def refresh_project_dir_cache(repo: StorageRepository) -> None:
    """Reload the project_dir → project_id cache from DB. Called at startup."""
    try:
        projects = await repo.list_projects()
    except Exception:
        return
    new_cache: dict[str, str] = {}
    for p in projects:
        rp = (p.root_path or "").replace("\\", "/").rstrip("/").lower()
        if rp:
            new_cache[rp] = p.id
    _project_dir_cache.clear()
    _project_dir_cache.update(new_cache)


def get_memory_store() -> MemoryStore:
    """Get MemoryStore instance, injected via FastAPI Depends()."""
    if _memory_store is None:
        msg = "MemoryStore not initialized, ensure application has started"
        raise RuntimeError(msg)
    return _memory_store


def get_event_bus() -> EventBus:
    """Get EventBus instance, injected via FastAPI Depends()."""
    if _event_bus is None:
        msg = "EventBus not initialized, ensure application has started"
        raise RuntimeError(msg)
    return _event_bus


def get_hook_translator() -> HookTranslator:
    """Get HookTranslator singleton, injected via FastAPI Depends()."""
    if _hook_translator is None:
        msg = "HookTranslator not initialized, ensure application has started"
        raise RuntimeError(msg)
    return _hook_translator


def get_reaper() -> "StateReaper | None":
    """Get the StateReaper singleton (owns the WakeAgentManager used by fleet dispatch).

    Returns None when the reaper has not started yet (e.g. very early startup), so
    callers must handle absence rather than assume it is always available.
    """
    return _reaper


def get_loop_engine() -> LoopEngine:
    """Get LoopEngine instance, injected via FastAPI Depends()."""
    if _loop_engine is None:
        msg = "LoopEngine not initialized, ensure application has started"
        raise RuntimeError(msg)
    return _loop_engine
