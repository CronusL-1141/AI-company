"""测试数据库连接管理 — PostgreSQL连接池参数验证 + EnginePool."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from aiteam.storage.connection import get_engine
from aiteam.storage.engine_pool import EnginePool, engine_pool


@pytest.fixture(autouse=True)
def _reset_engine_pool():
    """每个测试前后重置全局 EnginePool 缓存."""
    original_engines = engine_pool._engines.copy()
    original_factories = engine_pool._factories.copy()
    engine_pool._engines.clear()
    engine_pool._factories.clear()
    yield
    engine_pool._engines.clear()
    engine_pool._factories.clear()
    engine_pool._engines.update(original_engines)
    engine_pool._factories.update(original_factories)


class TestGetEnginePoolConfig:
    """验证 get_engine() 根据不同数据库URL传递正确的参数."""

    @patch("aiteam.storage.engine_pool.create_async_engine")
    def test_sqlite_uses_check_same_thread(self, mock_create: MagicMock) -> None:
        """SQLite URL 应传递 check_same_thread=False."""
        mock_create.return_value = MagicMock()
        mock_create.return_value.url = "sqlite+aiosqlite:///test.db"

        get_engine("sqlite+aiosqlite:///test.db")

        mock_create.assert_called_once()
        kwargs = mock_create.call_args
        assert kwargs[1]["connect_args"] == {"check_same_thread": False}
        # SQLite 不应有连接池参数
        assert "pool_size" not in kwargs[1]
        assert "max_overflow" not in kwargs[1]

    @patch("aiteam.storage.engine_pool.create_async_engine")
    def test_postgresql_uses_pool_config(self, mock_create: MagicMock) -> None:
        """PostgreSQL URL 应传递连接池参数."""
        mock_create.return_value = MagicMock()
        mock_create.return_value.url = "postgresql+asyncpg://user:pass@localhost/db"

        get_engine("postgresql+asyncpg://user:pass@localhost/db")

        mock_create.assert_called_once()
        kwargs = mock_create.call_args[1]
        assert kwargs["pool_size"] == 10
        assert kwargs["max_overflow"] == 20
        assert kwargs["pool_pre_ping"] is True
        assert kwargs["pool_recycle"] == 3600
        # PostgreSQL 不应有 SQLite 的 connect_args
        assert "connect_args" not in kwargs

    @patch("aiteam.storage.engine_pool.create_async_engine")
    def test_postgresql_no_check_same_thread(self, mock_create: MagicMock) -> None:
        """PostgreSQL URL 不应包含 check_same_thread 参数."""
        mock_create.return_value = MagicMock()
        mock_create.return_value.url = "postgresql+asyncpg://localhost/db"

        get_engine("postgresql+asyncpg://localhost/db")

        kwargs = mock_create.call_args[1]
        assert "connect_args" not in kwargs

    @patch("aiteam.storage.engine_pool.create_async_engine")
    def test_echo_always_false(self, mock_create: MagicMock) -> None:
        """所有引擎的 echo 参数应为 False."""
        mock_create.return_value = MagicMock()

        # SQLite
        mock_create.return_value.url = "sqlite+aiosqlite:///test.db"
        get_engine("sqlite+aiosqlite:///test.db")
        assert mock_create.call_args[1]["echo"] is False

        # 重置引擎缓存
        engine_pool._engines.clear()
        engine_pool._factories.clear()

        # PostgreSQL
        mock_create.return_value.url = "postgresql+asyncpg://localhost/db"
        get_engine("postgresql+asyncpg://localhost/db")
        assert mock_create.call_args[1]["echo"] is False


class TestGetEngineCache:
    """验证引擎缓存机制 — EnginePool."""

    @patch("aiteam.storage.engine_pool.create_async_engine")
    def test_same_url_returns_cached_engine(self, mock_create: MagicMock) -> None:
        """相同URL应返回缓存的引擎实例."""
        mock_engine = MagicMock()
        mock_engine.url = "sqlite+aiosqlite:///test.db"
        mock_create.return_value = mock_engine

        engine1 = get_engine("sqlite+aiosqlite:///test.db")
        engine2 = get_engine("sqlite+aiosqlite:///test.db")

        assert engine1 is engine2
        assert mock_create.call_count == 1

    @patch("aiteam.storage.engine_pool.create_async_engine")
    def test_different_url_creates_new_engine(self, mock_create: MagicMock) -> None:
        """不同URL应创建新引擎."""
        mock_engine1 = MagicMock()
        mock_engine1.url = "sqlite+aiosqlite:///test1.db"
        mock_engine2 = MagicMock()
        mock_engine2.url = "sqlite+aiosqlite:///test2.db"
        mock_create.side_effect = [mock_engine1, mock_engine2]

        get_engine("sqlite+aiosqlite:///test1.db")
        get_engine("sqlite+aiosqlite:///test2.db")

        assert mock_create.call_count == 2


class TestEnginePoolEviction:
    """验证 EnginePool 的 LRU 淘汰机制."""

    @patch("aiteam.storage.engine_pool.create_async_engine")
    def test_evicts_oldest_when_full(self, mock_create: MagicMock) -> None:
        """超过 max_size 时应淘汰最旧的引擎."""
        pool = EnginePool(max_size=2)

        engines = []
        for i in range(3):
            mock_eng = MagicMock()
            mock_eng.url = f"sqlite+aiosqlite:///test{i}.db"
            engines.append(mock_eng)
        mock_create.side_effect = engines

        pool.get_engine("sqlite+aiosqlite:///test0.db")
        pool.get_engine("sqlite+aiosqlite:///test1.db")
        pool.get_engine("sqlite+aiosqlite:///test2.db")

        # Pool should only have 2 engines (test1 and test2)
        assert len(pool._engines) == 2
        assert "sqlite+aiosqlite:///test0.db" not in pool._engines
        assert "sqlite+aiosqlite:///test1.db" in pool._engines
        assert "sqlite+aiosqlite:///test2.db" in pool._engines
        # Evicted engine should be in pending dispose
        assert len(pool._pending_dispose) == 1
