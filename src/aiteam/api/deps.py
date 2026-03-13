"""AI Team OS — API依赖注入.

提供 TeamManager 单例和 StorageRepository 的 lifespan 管理。
"""

from __future__ import annotations

import logging

from aiteam.api.event_bus import EventBus
from aiteam.api.state_reaper import StateReaper
from aiteam.memory.store import MemoryStore
from aiteam.orchestrator.team_manager import TeamManager
from aiteam.storage.connection import close_db
from aiteam.storage.repository import StorageRepository
from aiteam.types import AgentStatus

logger = logging.getLogger(__name__)

# 模块级单例
_repository: StorageRepository | None = None
_memory_store: MemoryStore | None = None
_event_bus: EventBus | None = None
_manager: TeamManager | None = None
_reaper: StateReaper | None = None


async def _startup_reconciliation(repo: StorageRepository) -> None:
    """启动对账 — OS重启时将所有BUSY agent设为IDLE.

    原理：OS重启意味着之前的CC session已不存在，
    所有残留的BUSY状态都是僵尸，需要清零。
    """
    teams = await repo.list_teams()
    reconciled = 0
    for team in teams:
        agents = await repo.list_agents(team.id)
        for agent in agents:
            if agent.status == AgentStatus.BUSY:
                await repo.update_agent(
                    agent.id, status=AgentStatus.IDLE.value, current_task=None,
                )
                reconciled += 1
    if reconciled > 0:
        logger.warning("启动对账: %d 个BUSY agent已重置为IDLE", reconciled)
    else:
        logger.info("启动对账: 无残留BUSY agent")


async def init_dependencies() -> None:
    """初始化所有依赖（lifespan startup时调用）."""
    global _repository, _memory_store, _event_bus, _manager, _reaper  # noqa: PLW0603

    _repository = StorageRepository()
    await _repository.init_db()
    _memory_store = MemoryStore(repository=_repository)
    _event_bus = EventBus(repo=_repository)
    _manager = TeamManager(
        repository=_repository, memory=_memory_store, event_bus=_event_bus,
    )

    # 启动对账：清除残留BUSY状态
    await _startup_reconciliation(_repository)

    # 启动StateReaper后台收割器
    _reaper = StateReaper(repo=_repository, event_bus=_event_bus)
    _reaper.start()


async def cleanup_dependencies() -> None:
    """清理所有依赖（lifespan shutdown时调用）."""
    global _repository, _memory_store, _event_bus, _manager, _reaper  # noqa: PLW0603

    # 先停止StateReaper
    if _reaper is not None:
        await _reaper.stop()
        _reaper = None

    await close_db()
    _repository = None
    _memory_store = None
    _event_bus = None
    _manager = None


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


def get_event_bus() -> EventBus:
    """获取 EventBus 实例，通过 FastAPI Depends() 注入."""
    if _event_bus is None:
        msg = "EventBus 尚未初始化，请确保应用已启动"
        raise RuntimeError(msg)
    return _event_bus
