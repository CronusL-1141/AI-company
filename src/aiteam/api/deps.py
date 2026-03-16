"""AI Team OS — API依赖注入.

提供 TeamManager 单例和 StorageRepository 的 lifespan 管理。
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

# 模块级单例
_repository: StorageRepository | None = None
_memory_store: MemoryStore | None = None
_event_bus: EventBus | None = None
_manager: TeamManager | None = None
_reaper: StateReaper | None = None
_watchdog_runner: WatchdogRunner | None = None
_hook_translator: HookTranslator | None = None
_loop_engine: LoopEngine | None = None


async def _run_migrations(db_url: str | None = None) -> None:
    """运行数据库迁移 — 为现有表添加新列.

    SQLAlchemy create_all 只创建不存在的表，不会 ALTER 已有表。
    此函数检查并添加 M3.1 新增的列。
    """
    engine = get_engine(db_url)

    # 需要添加的列: (表名, 列名, 列类型SQL)
    migrations: list[tuple[str, str, str]] = [
        ("teams", "project_id", "VARCHAR(36)"),
        ("teams", "status", "VARCHAR(20) DEFAULT 'active'"),
        ("agents", "project_id", "VARCHAR(36)"),
        ("agents", "current_phase_id", "VARCHAR(36)"),
        ("tasks", "project_id", "VARCHAR(36)"),
        ("tasks", "parent_id", "VARCHAR(36)"),
        ("tasks", "depends_on", "JSON DEFAULT '[]'"),
        ("tasks", "depth", "INTEGER DEFAULT 0"),
        ("tasks", "order", 'INTEGER DEFAULT 0'),
        ("tasks", "template_id", "VARCHAR(50)"),
        ("meetings", "project_id", "VARCHAR(36)"),
        ("tasks", "config", "JSON DEFAULT '{}'"),
    ]

    async with engine.connect() as conn:
        for table_name, col_name, col_type in migrations:
            # 检查列是否已存在
            has_column = await conn.run_sync(
                lambda sync_conn, t=table_name, c=col_name: c
                in [col["name"] for col in inspect(sync_conn).get_columns(t)]
                if inspect(sync_conn).has_table(t)
                else False
            )
            if not has_column:
                await conn.execute(
                    text(f"ALTER TABLE {table_name} ADD COLUMN {col_name} {col_type}")
                )
                logger.info("迁移: %s 表添加列 %s", table_name, col_name)

        # 值迁移: idle → waiting（三状态模型升级）
        await conn.execute(text("UPDATE agents SET status='waiting' WHERE status='idle'"))
        await conn.commit()


async def _auto_create_projects(repo: StorageRepository) -> None:
    """为没有 project_id 的 Team 自动创建对应 Project 并关联."""
    teams = await repo.list_teams()
    orphan_teams = [t for t in teams if not t.project_id]
    if not orphan_teams:
        return
    # 检查是否已有项目可以复用
    existing_projects = await repo.list_projects()
    if existing_projects:
        # 所有孤立Team归入第一个现有Project
        project = existing_projects[0]
        for team in orphan_teams:
            await repo.update_team(team.id, project_id=project.id)
        logger.info("将 %d 个孤立Team关联到已有Project: %s", len(orphan_teams), project.name)
    else:
        # 创建一个统一Project，用team_id做唯一root_path
        project = await repo.create_project(
            name="AI Team OS",
            root_path=f"auto-{orphan_teams[0].id}",
            description="Auto-created project",
        )
        for team in orphan_teams:
            await repo.update_team(team.id, project_id=project.id)
        logger.info("自动创建Project并关联 %d 个Team", len(orphan_teams))


async def _startup_reconciliation(repo: StorageRepository) -> None:
    """启动对账 — OS重启时将所有BUSY agent设为IDLE并清除session关联.

    原理：OS重启意味着之前的CC session已不存在，
    所有残留的BUSY状态和session_id都是僵尸，需要清零。
    """
    teams = await repo.list_teams()
    reconciled = 0
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
    if reconciled > 0:
        logger.warning("启动对账: %d 个agent已重置（状态+session清零）", reconciled)
    else:
        logger.info("启动对账: 无需重置")


async def init_dependencies() -> None:
    """初始化所有依赖（lifespan startup时调用）."""
    global _repository, _memory_store, _event_bus, _manager, _reaper, _watchdog_runner, _hook_translator, _loop_engine  # noqa: PLW0603

    _repository = StorageRepository()
    await _repository.init_db()

    # M3.1迁移：为已有表添加新列 + 创建projects表
    await _run_migrations()

    _memory_store = MemoryStore(repository=_repository)
    _event_bus = EventBus(repo=_repository)
    _manager = TeamManager(
        repository=_repository, memory=_memory_store, event_bus=_event_bus,
    )
    _hook_translator = HookTranslator(repo=_repository, event_bus=_event_bus)
    _loop_engine = LoopEngine(repo=_repository)

    # 启动对账：清除残留BUSY状态
    await _startup_reconciliation(_repository)

    # 为没有project_id的Team自动创建Project
    await _auto_create_projects(_repository)

    # 启动StateReaper后台收割器
    _reaper = StateReaper(repo=_repository, event_bus=_event_bus)
    _reaper.start()

    # 启动WatchdogRunner后台巡检
    _watchdog_checker = WatchdogChecker(repo=_repository)
    _watchdog_runner = WatchdogRunner(checker=_watchdog_checker, event_bus=_event_bus)
    _watchdog_runner.start()


async def cleanup_dependencies() -> None:
    """清理所有依赖（lifespan shutdown时调用）."""
    global _repository, _memory_store, _event_bus, _manager, _reaper, _watchdog_runner, _hook_translator, _loop_engine  # noqa: PLW0603

    # 先停止WatchdogRunner
    if _watchdog_runner is not None:
        await _watchdog_runner.stop()
        _watchdog_runner = None

    # 停止StateReaper
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
    """获取 TeamManager 实例，通过 FastAPI Depends() 注入."""
    if _manager is None:
        msg = "TeamManager 尚未初始化，请确保应用已启动"
        raise RuntimeError(msg)
    return _manager


def get_repository() -> StorageRepository:
    """获取 StorageRepository 实例."""
    if _repository is None:
        msg = "StorageRepository 尚未初始化"
        raise RuntimeError(msg)
    return _repository


def get_memory_store() -> MemoryStore:
    """获取 MemoryStore 实例，通过 FastAPI Depends() 注入."""
    if _memory_store is None:
        msg = "MemoryStore 尚未初始化，请确保应用已启动"
        raise RuntimeError(msg)
    return _memory_store


def get_event_bus() -> EventBus:
    """获取 EventBus 实例，通过 FastAPI Depends() 注入."""
    if _event_bus is None:
        msg = "EventBus 尚未初始化，请确保应用已启动"
        raise RuntimeError(msg)
    return _event_bus


def get_hook_translator() -> HookTranslator:
    """获取 HookTranslator 单例，通过 FastAPI Depends() 注入."""
    if _hook_translator is None:
        msg = "HookTranslator 尚未初始化，请确保应用已启动"
        raise RuntimeError(msg)
    return _hook_translator


def get_loop_engine() -> LoopEngine:
    """获取 LoopEngine 实例，通过 FastAPI Depends() 注入."""
    if _loop_engine is None:
        msg = "LoopEngine 尚未初始化，请确保应用已启动"
        raise RuntimeError(msg)
    return _loop_engine
