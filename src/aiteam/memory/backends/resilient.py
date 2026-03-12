"""AI Team OS — 弹性记忆后端（Circuit Breaker 降级）.

当 primary 后端连续失败超过阈值时，自动切换到 fallback 后端。
primary 恢复后自动切回。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from aiteam.types import Memory

if TYPE_CHECKING:
    from aiteam.memory.backends import MemoryBackend

logger = logging.getLogger(__name__)


class ResilientMemoryBackend:
    """带降级能力的记忆后端 — primary 失败时自动切换到 fallback.

    实现 Circuit Breaker 模式:
    - 正常状态: 使用 primary 后端
    - primary 连续失败 >= threshold 次: 熔断，切换到 fallback
    - 每次 fallback 调用成功后尝试探测 primary，恢复则切回
    """

    def __init__(
        self,
        primary: MemoryBackend,
        fallback: MemoryBackend,
        threshold: int = 3,
    ) -> None:
        self._primary = primary
        self._fallback = fallback
        self._threshold = threshold
        self._failure_count: int = 0
        self._circuit_open: bool = False
        self._call_count_since_open: int = 0
        # 每隔多少次 fallback 调用尝试探测 primary
        self._probe_interval: int = 5

    def _record_success(self) -> None:
        """记录 primary 成功，重置计数器."""
        self._failure_count = 0
        if self._circuit_open:
            logger.info("记忆后端 primary 已恢复，关闭熔断")
            self._circuit_open = False
            self._call_count_since_open = 0

    def _record_failure(self) -> None:
        """记录 primary 失败，达到阈值时打开熔断."""
        self._failure_count += 1
        if self._failure_count >= self._threshold and not self._circuit_open:
            logger.warning(
                "记忆后端 primary 连续失败 %d 次，触发熔断，切换到 fallback",
                self._failure_count,
            )
            self._circuit_open = True
            self._call_count_since_open = 0

    def _should_probe_primary(self) -> bool:
        """判断是否应该尝试探测 primary 恢复."""
        if not self._circuit_open:
            return False
        self._call_count_since_open += 1
        return self._call_count_since_open % self._probe_interval == 0

    async def create(
        self, scope: str, scope_id: str, content: str, metadata: dict | None = None
    ) -> Memory:
        """创建记忆，primary 失败时降级到 fallback."""
        if self._circuit_open and not self._should_probe_primary():
            return await self._fallback.create(scope, scope_id, content, metadata)
        try:
            result = await self._primary.create(scope, scope_id, content, metadata)
            self._record_success()
            return result
        except Exception as exc:
            logger.debug("primary create 失败: %s", exc)
            self._record_failure()
            return await self._fallback.create(scope, scope_id, content, metadata)

    async def search(
        self, scope: str, scope_id: str, query: str, limit: int = 5
    ) -> list[Memory]:
        """搜索记忆，primary 失败时降级到 fallback."""
        if self._circuit_open and not self._should_probe_primary():
            return await self._fallback.search(scope, scope_id, query, limit)
        try:
            result = await self._primary.search(scope, scope_id, query, limit)
            self._record_success()
            return result
        except Exception as exc:
            logger.debug("primary search 失败: %s", exc)
            self._record_failure()
            return await self._fallback.search(scope, scope_id, query, limit)

    async def list_all(self, scope: str, scope_id: str) -> list[Memory]:
        """列出所有记忆，primary 失败时降级到 fallback."""
        if self._circuit_open and not self._should_probe_primary():
            return await self._fallback.list_all(scope, scope_id)
        try:
            result = await self._primary.list_all(scope, scope_id)
            self._record_success()
            return result
        except Exception as exc:
            logger.debug("primary list_all 失败: %s", exc)
            self._record_failure()
            return await self._fallback.list_all(scope, scope_id)

    async def get(self, memory_id: str) -> Memory | None:
        """获取记忆，primary 失败时降级到 fallback."""
        if self._circuit_open and not self._should_probe_primary():
            return await self._fallback.get(memory_id)
        try:
            result = await self._primary.get(memory_id)
            self._record_success()
            return result
        except Exception as exc:
            logger.debug("primary get 失败: %s", exc)
            self._record_failure()
            return await self._fallback.get(memory_id)

    async def delete(self, memory_id: str) -> bool:
        """删除记忆，primary 失败时降级到 fallback."""
        if self._circuit_open and not self._should_probe_primary():
            return await self._fallback.delete(memory_id)
        try:
            result = await self._primary.delete(memory_id)
            self._record_success()
            return result
        except Exception as exc:
            logger.debug("primary delete 失败: %s", exc)
            self._record_failure()
            return await self._fallback.delete(memory_id)
