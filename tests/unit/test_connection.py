"""测试数据库连接管理 — PostgreSQL连接池参数验证."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from aiteam.storage.connection import get_engine, _engine


@pytest.fixture(autouse=True)
def _reset_engine():
    """每个测试前后重置模块级引擎缓存."""
    import aiteam.storage.connection as conn_mod

    original = conn_mod._engine
    conn_mod._engine = None
    yield
    conn_mod._engine = original


class TestGetEnginePoolConfig:
    """验证 get_engine() 根据不同数据库URL传递正确的参数."""

    @patch("aiteam.storage.connection.create_async_engine")
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

    @patch("aiteam.storage.connection.create_async_engine")
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

    @patch("aiteam.storage.connection.create_async_engine")
    def test_postgresql_no_check_same_thread(self, mock_create: MagicMock) -> None:
        """PostgreSQL URL 不应包含 check_same_thread 参数."""
        mock_create.return_value = MagicMock()
        mock_create.return_value.url = "postgresql+asyncpg://localhost/db"

        get_engine("postgresql+asyncpg://localhost/db")

        kwargs = mock_create.call_args[1]
        assert "connect_args" not in kwargs

    @patch("aiteam.storage.connection.create_async_engine")
    def test_echo_always_false(self, mock_create: MagicMock) -> None:
        """所有引擎的 echo 参数应为 False."""
        mock_create.return_value = MagicMock()

        # SQLite
        mock_create.return_value.url = "sqlite+aiosqlite:///test.db"
        get_engine("sqlite+aiosqlite:///test.db")
        assert mock_create.call_args[1]["echo"] is False

        # 重置引擎缓存
        import aiteam.storage.connection as conn_mod
        conn_mod._engine = None

        # PostgreSQL
        mock_create.return_value.url = "postgresql+asyncpg://localhost/db"
        get_engine("postgresql+asyncpg://localhost/db")
        assert mock_create.call_args[1]["echo"] is False


class TestGetEngineCache:
    """验证引擎缓存机制."""

    @patch("aiteam.storage.connection.create_async_engine")
    def test_same_url_returns_cached_engine(self, mock_create: MagicMock) -> None:
        """相同URL应返回缓存的引擎实例."""
        mock_engine = MagicMock()
        mock_engine.url = "sqlite+aiosqlite:///test.db"
        mock_create.return_value = mock_engine

        engine1 = get_engine("sqlite+aiosqlite:///test.db")
        engine2 = get_engine("sqlite+aiosqlite:///test.db")

        assert engine1 is engine2
        assert mock_create.call_count == 1

    @patch("aiteam.storage.connection.create_async_engine")
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
