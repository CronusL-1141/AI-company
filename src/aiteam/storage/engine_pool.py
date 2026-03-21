"""AI Team OS — Multi-DB engine pool.

Manages multiple SQLAlchemy async engines keyed by database URL,
enabling per-project database isolation with LRU eviction.
"""

from __future__ import annotations

import logging
from collections import OrderedDict
from typing import Any

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

logger = logging.getLogger(__name__)


class EnginePool:
    """Pool of SQLAlchemy async engines, keyed by database URL.

    Uses LRU eviction when the pool exceeds max_size.
    Each unique db_url gets its own engine and session factory.
    """

    def __init__(self, max_size: int = 20) -> None:
        self._engines: OrderedDict[str, AsyncEngine] = OrderedDict()
        self._factories: dict[str, async_sessionmaker[AsyncSession]] = {}
        self._max_size = max_size
        self._pending_dispose: list[AsyncEngine] = []

    def get_engine(self, db_url: str) -> AsyncEngine:
        """Get or create an engine for the given database URL.

        Args:
            db_url: SQLAlchemy database URL.

        Returns:
            AsyncEngine instance.
        """
        if db_url in self._engines:
            self._engines.move_to_end(db_url)
            return self._engines[db_url]

        # Evict oldest engine if pool is full
        if len(self._engines) >= self._max_size:
            oldest_url, oldest_engine = self._engines.popitem(last=False)
            self._factories.pop(oldest_url, None)
            self._pending_dispose.append(oldest_engine)
            logger.info("EnginePool: evicted engine for %s", oldest_url[:60])

        kwargs: dict[str, Any] = {"echo": False}
        if "sqlite" in db_url:
            kwargs["connect_args"] = {"check_same_thread": False}
        elif "postgresql" in db_url:
            kwargs["pool_size"] = 10
            kwargs["max_overflow"] = 20
            kwargs["pool_pre_ping"] = True
            kwargs["pool_recycle"] = 3600

        engine = create_async_engine(db_url, **kwargs)
        self._engines[db_url] = engine
        return engine

    def get_session_factory(self, db_url: str) -> async_sessionmaker[AsyncSession]:
        """Get or create a session factory for the given database URL.

        Args:
            db_url: SQLAlchemy database URL.

        Returns:
            async_sessionmaker instance.
        """
        if db_url not in self._factories:
            engine = self.get_engine(db_url)
            self._factories[db_url] = async_sessionmaker(engine, expire_on_commit=False)
        return self._factories[db_url]

    async def dispose_all(self) -> None:
        """Dispose all engines and clear the pool."""
        # Dispose pending evicted engines
        for engine in self._pending_dispose:
            try:
                await engine.dispose()
            except Exception:
                pass
        self._pending_dispose.clear()

        # Dispose all active engines
        for engine in self._engines.values():
            try:
                await engine.dispose()
            except Exception:
                pass
        self._engines.clear()
        self._factories.clear()

    async def dispose_pending(self) -> None:
        """Dispose only evicted engines (call periodically to free resources)."""
        for engine in self._pending_dispose:
            try:
                await engine.dispose()
            except Exception:
                pass
        self._pending_dispose.clear()


# Global engine pool instance
engine_pool = EnginePool()
