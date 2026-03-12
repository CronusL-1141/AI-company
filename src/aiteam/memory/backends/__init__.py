"""AI Team OS — 记忆后端抽象层.

定义 MemoryBackend Protocol 及各后端实现。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from aiteam.types import Memory


@runtime_checkable
class MemoryBackend(Protocol):
    """记忆存储后端抽象接口.

    所有后端实现（SQLite、Mem0等）都必须遵循此协议。
    """

    async def create(
        self, scope: str, scope_id: str, content: str, metadata: dict | None = None
    ) -> Memory:
        """创建记忆."""
        ...

    async def search(
        self, scope: str, scope_id: str, query: str, limit: int = 5
    ) -> list[Memory]:
        """搜索记忆."""
        ...

    async def list_all(self, scope: str, scope_id: str) -> list[Memory]:
        """列出指定作用域的所有记忆."""
        ...

    async def get(self, memory_id: str) -> Memory | None:
        """根据ID获取记忆."""
        ...

    async def delete(self, memory_id: str) -> bool:
        """删除记忆."""
        ...
