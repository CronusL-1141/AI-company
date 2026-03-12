"""AI Team OS — API依赖注入.

提供 TeamManager 单例和 StorageRepository 的 lifespan 管理。
"""

from __future__ import annotations

from aiteam.memory.store import MemoryStore
from aiteam.orchestrator.team_manager import TeamManager
from aiteam.storage.connection import close_db
from aiteam.storage.repository import StorageRepository

# 模块级单例
_repository: StorageRepository | None = None
_memory_store: MemoryStore | None = None
_manager: TeamManager | None = None


async def init_dependencies() -> None:
    """初始化所有依赖（lifespan startup时调用）."""
    global _repository, _memory_store, _manager  # noqa: PLW0603

    _repository = StorageRepository()
    await _repository.init_db()
    _memory_store = MemoryStore(repository=_repository)
    _manager = TeamManager(repository=_repository, memory=_memory_store)


async def cleanup_dependencies() -> None:
    """清理所有依赖（lifespan shutdown时调用）."""
    global _repository, _memory_store, _manager  # noqa: PLW0603
    await close_db()
    _repository = None
    _memory_store = None
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
