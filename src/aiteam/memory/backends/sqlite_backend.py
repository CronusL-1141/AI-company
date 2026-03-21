"""AI Team OS — SQLite memory backend.

Wraps the existing StorageRepository as a MemoryBackend implementation,
maintaining full compatibility with M1.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from aiteam.types import Memory

if TYPE_CHECKING:
    from aiteam.storage.repository import StorageRepository


class SqliteMemoryBackend:
    """SQLite memory backend — wraps StorageRepository."""

    def __init__(self, repository: StorageRepository) -> None:
        self._repo = repository

    async def create(
        self, scope: str, scope_id: str, content: str, metadata: dict | None = None
    ) -> Memory:
        """Create a memory, delegating to StorageRepository."""
        return await self._repo.create_memory(scope, scope_id, content, metadata)

    async def search(
        self, scope: str, scope_id: str, query: str, limit: int = 5
    ) -> list[Memory]:
        """Search memories, delegating to StorageRepository."""
        return await self._repo.search_memories(scope, scope_id, query, limit)

    async def list_all(self, scope: str, scope_id: str) -> list[Memory]:
        """List all memories, delegating to StorageRepository."""
        return await self._repo.list_memories(scope, scope_id)

    async def get(self, memory_id: str) -> Memory | None:
        """Get a memory by ID, delegating to StorageRepository."""
        return await self._repo.get_memory(memory_id)

    async def delete(self, memory_id: str) -> bool:
        """Delete a memory, delegating to StorageRepository."""
        return await self._repo.delete_memory(memory_id)
