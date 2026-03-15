"""AI Team OS — 异步数据库连接管理.

提供 SQLAlchemy 异步引擎和会话管理，支持自动建表。
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from aiteam.storage.models import Base

def _migrate_old_db_if_needed(new_db_path: Path) -> None:
    """检测旧DB并自动迁移到新路径。

    仅在新DB不存在或很小(<10KB)时，从旧位置复制数据库文件。
    迁移后旧文件重命名为 .db.migrated 避免重复迁移。
    所有错误静默处理，不阻塞启动。
    """
    try:
        if new_db_path.exists() and new_db_path.stat().st_size > 10000:
            return  # 新DB已有数据，不迁移

        old_candidates = [
            Path.cwd() / "aiteam.db",
        ]

        for old_path in old_candidates:
            if old_path.exists() and old_path.stat().st_size > 10000:
                import shutil
                shutil.copy2(str(old_path), str(new_db_path))
                old_path.rename(old_path.with_suffix(".db.migrated"))
                break
    except Exception:
        pass  # 静默处理，不阻塞启动


def _default_db_url() -> str:
    """构建默认数据库URL，使用固定路径 ~/.claude/data/ai-team-os/aiteam.db."""
    data_dir = Path.home() / ".claude" / "data" / "ai-team-os"
    data_dir.mkdir(parents=True, exist_ok=True)
    new_db_path = data_dir / "aiteam.db"
    _migrate_old_db_if_needed(new_db_path)
    return f"sqlite+aiosqlite:///{new_db_path}"


# 默认数据库URL
DEFAULT_DB_URL = _default_db_url()

# 模块级别的引擎和会话工厂缓存
_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def get_engine(db_url: str | None = None) -> AsyncEngine:
    """获取或创建异步数据库引擎.

    Args:
        db_url: 数据库连接URL，为空时使用默认值。

    Returns:
        AsyncEngine 实例。
    """
    global _engine
    url = db_url or DEFAULT_DB_URL
    if _engine is None or str(_engine.url) != url:
        kwargs: dict[str, Any] = {"echo": False}
        if "sqlite" in url:
            # SQLite 特有设置
            kwargs["connect_args"] = {"check_same_thread": False}
        elif "postgresql" in url:
            # PostgreSQL 连接池配置
            kwargs["pool_size"] = 10
            kwargs["max_overflow"] = 20
            kwargs["pool_pre_ping"] = True
            kwargs["pool_recycle"] = 3600
        _engine = create_async_engine(url, **kwargs)
    return _engine


def get_session_factory(
    engine: AsyncEngine | None = None,
) -> async_sessionmaker[AsyncSession]:
    """获取或创建异步会话工厂.

    Args:
        engine: 可选的引擎实例，为空时使用默认引擎。

    Returns:
        async_sessionmaker 实例。
    """
    global _session_factory
    eng = engine or get_engine()
    # 当引擎变更时重建会话工厂，确保测试隔离
    if _session_factory is None:
        _session_factory = async_sessionmaker(eng, expire_on_commit=False)
    return _session_factory


@asynccontextmanager
async def get_session(
    db_url: str | None = None,
) -> AsyncGenerator[AsyncSession, None]:
    """获取异步数据库会话的上下文管理器.

    用法:
        async with get_session() as session:
            result = await session.execute(...)

    Args:
        db_url: 可选的数据库URL。

    Yields:
        AsyncSession 实例。
    """
    engine = get_engine(db_url)
    factory = get_session_factory(engine)
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def init_db(db_url: str | None = None) -> None:
    """初始化数据库，创建所有表.

    如果使用 SQLite 且数据库文件的父目录不存在，会自动创建目录。

    Args:
        db_url: 数据库连接URL。
    """
    url = db_url or DEFAULT_DB_URL

    # 确保 SQLite 数据库文件的目录存在
    if "sqlite" in url:
        # 从 URL 提取文件路径: sqlite+aiosqlite:///path/to/db
        db_path_str = url.split("///", 1)[-1] if "///" in url else ""
        if db_path_str:
            db_path = Path(db_path_str)
            db_path.parent.mkdir(parents=True, exist_ok=True)

    engine = get_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def close_db() -> None:
    """关闭数据库连接，释放资源."""
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _session_factory = None
