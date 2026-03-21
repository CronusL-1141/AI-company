"""AI Team OS — Mem0 memory backend.

Accesses the Mem0 service via the mem0ai SDK.
The mem0 import is lazy — it is only imported when this backend is actually instantiated.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import uuid4

from aiteam.types import Memory, MemoryScope


def _scope_to_mem0_params(scope: str, scope_id: str) -> dict[str, str]:
    """Map four-level scopes to Mem0's user_id / agent_id parameters.

    Mapping rules:
    - global -> user_id="__global__"
    - team:{id} -> user_id="team_{id}"
    - agent:{id} -> agent_id="{id}"
    - user:{id} -> user_id="{id}"
    """
    if scope == MemoryScope.GLOBAL.value:
        return {"user_id": "__global__"}
    elif scope == MemoryScope.TEAM.value:
        return {"user_id": f"team_{scope_id}"}
    elif scope == MemoryScope.AGENT.value:
        return {"agent_id": scope_id}
    else:
        # user or other
        return {"user_id": scope_id}


def _mem0_result_to_memory(item: dict[str, Any], scope: str, scope_id: str) -> Memory:
    """Convert a Mem0 result item to a Memory object."""
    return Memory(
        id=str(item.get("id", uuid4())),
        scope=MemoryScope(scope),
        scope_id=scope_id,
        content=str(item.get("memory", item.get("text", ""))),
        metadata=item.get("metadata", {}),
        created_at=datetime.fromisoformat(item["created_at"])
        if "created_at" in item
        else datetime.now(),
        accessed_at=datetime.now(),
    )


class Mem0MemoryBackend:
    """Mem0 memory backend — accessed via the mem0ai SDK.

    The mem0 dependency is optional and only imported when this backend is instantiated.
    A friendly ImportError is raised if mem0ai is not installed.
    """

    def __init__(self, config: dict | None = None) -> None:
        try:
            from mem0 import Memory as Mem0Memory
        except ImportError:
            raise ImportError(
                "使用 Mem0 后端需要安装 mem0ai 包: pip install mem0ai"
            )
        self._mem0 = Mem0Memory.from_config(config or {})

    async def create(
        self, scope: str, scope_id: str, content: str, metadata: dict | None = None
    ) -> Memory:
        """Create a memory via the Mem0 SDK."""
        params = _scope_to_mem0_params(scope, scope_id)
        result = self._mem0.add(content, **params, metadata=metadata or {})

        # mem0.add may return a dict or list
        if isinstance(result, list) and len(result) > 0:
            item = result[0]
        elif isinstance(result, dict):
            # Newer mem0 returns {"results": [...]}
            results = result.get("results", [result])
            item = results[0] if results else result
        else:
            item = {}

        memory_id = str(item.get("id", uuid4()))
        return Memory(
            id=memory_id,
            scope=MemoryScope(scope),
            scope_id=scope_id,
            content=content,
            metadata=metadata or {},
        )

    async def search(
        self, scope: str, scope_id: str, query: str, limit: int = 5
    ) -> list[Memory]:
        """Search memories via the Mem0 SDK."""
        params = _scope_to_mem0_params(scope, scope_id)
        results = self._mem0.search(query, **params, limit=limit)

        # Result may be a list or dict{"results": [...]}
        if isinstance(results, dict):
            items = results.get("results", [])
        else:
            items = results or []

        return [_mem0_result_to_memory(item, scope, scope_id) for item in items]

    async def list_all(self, scope: str, scope_id: str) -> list[Memory]:
        """List all memories via the Mem0 SDK."""
        params = _scope_to_mem0_params(scope, scope_id)
        results = self._mem0.get_all(**params)

        if isinstance(results, dict):
            items = results.get("results", [])
        else:
            items = results or []

        return [_mem0_result_to_memory(item, scope, scope_id) for item in items]

    async def get(self, memory_id: str) -> Memory | None:
        """Get a single memory via the Mem0 SDK."""
        try:
            result = self._mem0.get(memory_id)
        except Exception:
            return None

        if not result:
            return None

        # result may be a single dict
        if isinstance(result, dict):
            scope_str = result.get("metadata", {}).get("scope", "global")
            scope_id = result.get("metadata", {}).get("scope_id", "system")
            return _mem0_result_to_memory(result, scope_str, scope_id)

        return None

    async def delete(self, memory_id: str) -> bool:
        """Delete a memory via the Mem0 SDK."""
        try:
            self._mem0.delete(memory_id)
            return True
        except Exception:
            return False
