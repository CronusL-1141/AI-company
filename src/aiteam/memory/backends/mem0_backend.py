"""AI Team OS — Mem0 记忆后端.

通过 mem0ai SDK 访问 Mem0 服务。
mem0 的导入是 lazy 的，只在实际实例化时才导入。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import uuid4

from aiteam.types import Memory, MemoryScope


def _scope_to_mem0_params(scope: str, scope_id: str) -> dict[str, str]:
    """将四级作用域映射为 Mem0 的 user_id / agent_id 参数.

    映射规则:
    - global → user_id="__global__"
    - team:{id} → user_id="team_{id}"
    - agent:{id} → agent_id="{id}"
    - user:{id} → user_id="{id}"
    """
    if scope == MemoryScope.GLOBAL.value:
        return {"user_id": "__global__"}
    elif scope == MemoryScope.TEAM.value:
        return {"user_id": f"team_{scope_id}"}
    elif scope == MemoryScope.AGENT.value:
        return {"agent_id": scope_id}
    else:
        # user 或其他
        return {"user_id": scope_id}


def _mem0_result_to_memory(item: dict[str, Any], scope: str, scope_id: str) -> Memory:
    """将 Mem0 返回的结果项转换为 Memory 对象."""
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
    """Mem0 记忆后端 — 通过 mem0ai SDK 访问.

    mem0 依赖是可选的，只在实例化此后端时才导入。
    未安装时会抛出友好的 ImportError 提示。
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
        """通过 Mem0 SDK 创建记忆."""
        params = _scope_to_mem0_params(scope, scope_id)
        result = self._mem0.add(content, **params, metadata=metadata or {})

        # mem0.add 返回的结构可能是 dict 或 list
        if isinstance(result, list) and len(result) > 0:
            item = result[0]
        elif isinstance(result, dict):
            # 新版 mem0 返回 {"results": [...]}
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
        """通过 Mem0 SDK 搜索记忆."""
        params = _scope_to_mem0_params(scope, scope_id)
        results = self._mem0.search(query, **params, limit=limit)

        # 结果可能是 list 或 dict{"results": [...]}
        if isinstance(results, dict):
            items = results.get("results", [])
        else:
            items = results or []

        return [_mem0_result_to_memory(item, scope, scope_id) for item in items]

    async def list_all(self, scope: str, scope_id: str) -> list[Memory]:
        """通过 Mem0 SDK 列出所有记忆."""
        params = _scope_to_mem0_params(scope, scope_id)
        results = self._mem0.get_all(**params)

        if isinstance(results, dict):
            items = results.get("results", [])
        else:
            items = results or []

        return [_mem0_result_to_memory(item, scope, scope_id) for item in items]

    async def get(self, memory_id: str) -> Memory | None:
        """通过 Mem0 SDK 获取单条记忆."""
        try:
            result = self._mem0.get(memory_id)
        except Exception:
            return None

        if not result:
            return None

        # result 可能是单个 dict
        if isinstance(result, dict):
            scope_str = result.get("metadata", {}).get("scope", "global")
            scope_id = result.get("metadata", {}).get("scope_id", "system")
            return _mem0_result_to_memory(result, scope_str, scope_id)

        return None

    async def delete(self, memory_id: str) -> bool:
        """通过 Mem0 SDK 删除记忆."""
        try:
            self._mem0.delete(memory_id)
            return True
        except Exception:
            return False
