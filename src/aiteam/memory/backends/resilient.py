"""AI Team OS — Resilient memory backend (Circuit Breaker fallback).

When the primary backend fails consecutively beyond the threshold,
automatically switches to the fallback backend.
Switches back when the primary recovers.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from aiteam.types import Memory

if TYPE_CHECKING:
    from aiteam.memory.backends import MemoryBackend

logger = logging.getLogger(__name__)


class ResilientMemoryBackend:
    """Memory backend with fallback capability — auto-switches to fallback on primary failure.

    Implements the Circuit Breaker pattern:
    - Normal state: uses the primary backend
    - Primary fails consecutively >= threshold times: circuit opens, switches to fallback
    - Periodically probes the primary after fallback calls; switches back on recovery
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
        # Probe the primary every N fallback calls
        self._probe_interval: int = 5

    def _record_success(self) -> None:
        """Record primary success and reset counters."""
        self._failure_count = 0
        if self._circuit_open:
            logger.info("记忆后端 primary 已恢复，关闭熔断")
            self._circuit_open = False
            self._call_count_since_open = 0

    def _record_failure(self) -> None:
        """Record primary failure; open the circuit when threshold is reached."""
        self._failure_count += 1
        if self._failure_count >= self._threshold and not self._circuit_open:
            logger.warning(
                "记忆后端 primary 连续失败 %d 次，触发熔断，切换到 fallback",
                self._failure_count,
            )
            self._circuit_open = True
            self._call_count_since_open = 0

    def _should_probe_primary(self) -> bool:
        """Determine whether to probe the primary for recovery."""
        if not self._circuit_open:
            return False
        self._call_count_since_open += 1
        return self._call_count_since_open % self._probe_interval == 0

    async def create(
        self, scope: str, scope_id: str, content: str, metadata: dict | None = None
    ) -> Memory:
        """Create a memory; falls back on primary failure."""
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
        """Search memories; falls back on primary failure."""
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
        """List all memories; falls back on primary failure."""
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
        """Get a memory; falls back on primary failure."""
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
        """Delete a memory; falls back on primary failure."""
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
