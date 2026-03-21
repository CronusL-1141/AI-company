"""AI Team OS — Memory backends abstraction layer.

Defines the MemoryBackend Protocol and backend implementations.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from aiteam.types import Memory


@runtime_checkable
class MemoryBackend(Protocol):
    """Abstract interface for memory storage backends.

    All backend implementations (SQLite, Mem0, etc.) must conform to this protocol.
    """

    async def create(
        self, scope: str, scope_id: str, content: str, metadata: dict | None = None
    ) -> Memory:
        """Create a memory."""
        ...

    async def search(
        self, scope: str, scope_id: str, query: str, limit: int = 5
    ) -> list[Memory]:
        """Search memories."""
        ...

    async def list_all(self, scope: str, scope_id: str) -> list[Memory]:
        """List all memories for a given scope."""
        ...

    async def get(self, memory_id: str) -> Memory | None:
        """Get a memory by ID."""
        ...

    async def delete(self, memory_id: str) -> bool:
        """Delete a memory."""
        ...
