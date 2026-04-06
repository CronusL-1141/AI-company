"""AI Team OS — Three-temperature memory management.

Implements a Hot (in-memory cache) / Warm (MemoryBackend) / Cold (JSON archive) three-tier memory architecture.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from aiteam.memory.retriever import bm25_search, build_context_string
from aiteam.types import Memory, MemoryScope

if TYPE_CHECKING:
    from aiteam.memory.backends import MemoryBackend
    from aiteam.storage.repository import StorageRepository


class MemoryStore:
    """Three-temperature memory management.

    - Hot tier: Python dict in-memory cache, indexed by scope:scope_id
    - Warm tier: Operated via MemoryBackend (SQLite/Mem0/resilient backend, etc.)
    - Cold tier: JSON file archive
    """

    def __init__(
        self,
        backend: MemoryBackend | StorageRepository | None = None,
        repository: StorageRepository | None = None,
        archive_dir: Path | None = None,
    ) -> None:
        from aiteam.memory.backends import MemoryBackend as _MemoryBackend
        from aiteam.memory.backends.sqlite_backend import SqliteMemoryBackend
        from aiteam.storage.repository import StorageRepository as _StorageRepository

        # Backward compatibility: MemoryStore(repo) or MemoryStore(repository=repo)
        if backend is not None and isinstance(backend, _StorageRepository):
            repository = backend
            backend = None

        if backend is not None:
            self._backend: _MemoryBackend = backend  # type: ignore[assignment]
        elif repository is not None:
            self._backend = SqliteMemoryBackend(repository)
        else:
            raise ValueError("必须提供 backend 或 repository")

        self._archive_dir = archive_dir or Path(".aiteam/archive")
        # Hot tier cache: key = "scope:scope_id", value = list of Memory
        self._hot_cache: dict[str, list[Memory]] = {}

    def _cache_key(self, scope: str, scope_id: str) -> str:
        """Generate cache key."""
        return f"{scope}:{scope_id}"

    def _add_to_hot(self, memory: Memory) -> None:
        """Add a memory to the hot tier cache."""
        key = self._cache_key(memory.scope.value, memory.scope_id)
        if key not in self._hot_cache:
            self._hot_cache[key] = []
        self._hot_cache[key].append(memory)

    def _remove_from_hot(self, memory_id: str) -> bool:
        """Remove a memory from the hot tier cache."""
        for key, memories in self._hot_cache.items():
            for i, mem in enumerate(memories):
                if mem.id == memory_id:
                    memories.pop(i)
                    return True
        return False

    async def store(
        self,
        scope: str,
        scope_id: str,
        content: str,
        metadata: dict | None = None,
    ) -> str:
        """Store a memory to both hot and warm tiers, return memory_id.

        Args:
            scope: Memory scope (global/team/agent/user).
            scope_id: Scope ID.
            content: Memory content.
            metadata: Optional metadata.

        Returns:
            ID of the newly created memory.
        """
        # Warm tier: persist via backend
        memory = await self._backend.create(scope, scope_id, content, metadata)
        # Hot tier: add to in-memory cache
        self._add_to_hot(memory)
        return memory.id

    async def retrieve(
        self,
        scope: str,
        scope_id: str,
        query: str,
        limit: int = 5,
    ) -> list[Memory]:
        """Retrieve relevant memories.

        Searches the hot tier first, falling back to the warm tier if insufficient.
        Uses keyword matching in the M1 phase.

        Args:
            scope: Memory scope.
            scope_id: Scope ID.
            query: Search query.
            limit: Maximum number of results.

        Returns:
            List of relevant memories.
        """
        key = self._cache_key(scope, scope_id)

        # Search hot tier first — use BM25 when available, fallback to keyword_search
        hot_memories = self._hot_cache.get(key, [])
        if hot_memories:
            results = bm25_search(hot_memories, query)
            if len(results) >= limit:
                return results[:limit]

        # Hot tier insufficient, query warm tier
        warm_results = await self._backend.search(scope, scope_id, query, limit)

        # Merge and deduplicate (by memory_id)
        seen_ids: set[str] = set()
        merged: list[Memory] = []

        # Hot tier results take priority
        if hot_memories:
            for mem in bm25_search(hot_memories, query):
                if mem.id not in seen_ids:
                    seen_ids.add(mem.id)
                    merged.append(mem)

        # Supplement with warm tier results
        for mem in warm_results:
            if mem.id not in seen_ids:
                seen_ids.add(mem.id)
                merged.append(mem)

        return merged[:limit]

    async def get_context(self, agent_id: str, task: str) -> str:
        """Build a context string for an agent.

        Retrieves agent memories + team memories + global memories and concatenates them.

        Args:
            agent_id: The agent's ID.
            task: Current task description (used as the retrieval query).

        Returns:
            Formatted context string.
        """
        all_memories: list[Memory] = []

        # Retrieve agent-level memories
        agent_memories = await self.retrieve(MemoryScope.AGENT.value, agent_id, task, limit=5)
        all_memories.extend(agent_memories)

        # Retrieve global-level memories
        global_memories = await self.retrieve(MemoryScope.GLOBAL.value, "system", task, limit=3)
        all_memories.extend(global_memories)

        return build_context_string(all_memories)

    async def list_all(self, scope: str, scope_id: str) -> list[Memory]:
        """List all memories for a given scope.

        Args:
            scope: Memory scope.
            scope_id: Scope ID.

        Returns:
            List of all memories under this scope.
        """
        return await self._backend.list_all(scope, scope_id)

    async def delete(self, memory_id: str) -> bool:
        """Delete a memory from both hot and warm tiers.

        Args:
            memory_id: ID of the memory to delete.

        Returns:
            Whether the deletion was successful.
        """
        # Remove from hot tier
        self._remove_from_hot(memory_id)
        # Remove from warm tier
        return await self._backend.delete(memory_id)

    async def archive(self, scope: str, scope_id: str) -> Path:
        """Export warm tier memories to a JSON file in the cold tier.

        Args:
            scope: Memory scope.
            scope_id: Scope ID.

        Returns:
            Path to the archive file.
        """
        # Get all memories from warm tier
        memories = await self._backend.list_all(scope, scope_id)

        # Build archive directory
        archive_path = self._archive_dir / scope / scope_id
        archive_path.mkdir(parents=True, exist_ok=True)

        # Generate archive file
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        file_path = archive_path / f"{timestamp}.json"

        data = [
            {
                "id": mem.id,
                "scope": mem.scope.value,
                "scope_id": mem.scope_id,
                "content": mem.content,
                "metadata": mem.metadata,
                "created_at": mem.created_at.isoformat(),
                "accessed_at": mem.accessed_at.isoformat(),
            }
            for mem in memories
        ]

        file_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return file_path
