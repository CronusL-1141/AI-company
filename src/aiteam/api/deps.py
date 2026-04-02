"""AI Team OS — API dependency injection.

Provides TeamManager singleton and StorageRepository lifespan management.
"""

from __future__ import annotations

import logging

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
    ]

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


async def _auto_create_projects(repo: StorageRepository) -> None:
    """Auto-create Projects for Teams without project_id and link them."""
    teams = await repo.list_teams()
    orphan_teams = [t for t in teams if not t.project_id]
    if not orphan_teams:
        return
    # Check if existing projects can be reused
    existing_projects = await repo.list_projects()
    if existing_projects:
        # Assign all orphan Teams to the first existing Project
        project = existing_projects[0]
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

    # M3.1 migration: add new columns to existing tables + create projects table
    await _run_migrations()

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


def get_loop_engine() -> LoopEngine:
    """Get LoopEngine instance, injected via FastAPI Depends()."""
    if _loop_engine is None:
        msg = "LoopEngine not initialized, ensure application has started"
        raise RuntimeError(msg)
    return _loop_engine
