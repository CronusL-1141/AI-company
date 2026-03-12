"""AI Team OS — SQLite 记忆后端.

将现有 StorageRepository 包装为 MemoryBackend 实现，
保持与 M1 的完全兼容。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from aiteam.types import Memory

if TYPE_CHECKING:
    from aiteam.storage.repository import StorageRepository


class SqliteMemoryBackend:
    """SQLite 记忆后端 — 包装 StorageRepository."""

    def __init__(self, repository: StorageRepository) -> None:
        self._repo = repository

    async def create(
        self, scope: str, scope_id: str, content: str, metadata: dict | None = None
    ) -> Memory:
        """创建记忆，委托给 StorageRepository."""
        return await self._repo.create_memory(scope, scope_id, content, metadata)

    async def search(
        self, scope: str, scope_id: str, query: str, limit: int = 5
    ) -> list[Memory]:
        """搜索记忆，委托给 StorageRepository."""
        return await self._repo.search_memories(scope, scope_id, query, limit)

    async def list_all(self, scope: str, scope_id: str) -> list[Memory]:
        """列出所有记忆，委托给 StorageRepository."""
        return await self._repo.list_memories(scope, scope_id)

    async def get(self, memory_id: str) -> Memory | None:
        """根据ID获取记忆，委托给 StorageRepository."""
        return await self._repo.get_memory(memory_id)

    async def delete(self, memory_id: str) -> bool:
        """删除记忆，委托给 StorageRepository."""
        return await self._repo.delete_memory(memory_id)
