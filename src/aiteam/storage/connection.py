"""AI Team OS — Async database connection management.

Provides SQLAlchemy async engine and session management with automatic table creation.
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
    """Detect old DB and auto-migrate to new path.

    Only copies the database file from the old location when the new DB
    does not exist or is very small (<10KB).
    After migration, the old file is renamed to .db.migrated to avoid repeated migrations.
    All errors are silently handled to not block startup.
    """
    try:
        if new_db_path.exists() and new_db_path.stat().st_size > 10000:
            return  # New DB already has data, skip migration

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
        pass  # Silent handling, do not block startup


def _default_db_url() -> str:
    """Build the default database URL, using fixed path ~/.claude/data/ai-team-os/aiteam.db."""
    data_dir = Path.home() / ".claude" / "data" / "ai-team-os"
    data_dir.mkdir(parents=True, exist_ok=True)
    new_db_path = data_dir / "aiteam.db"
    _migrate_old_db_if_needed(new_db_path)
    return f"sqlite+aiosqlite:///{new_db_path}"


# Default database URL
DEFAULT_DB_URL = _default_db_url()

# Module-level engine and session factory cache
_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def get_engine(db_url: str | None = None) -> AsyncEngine:
    """Get or create an async database engine.

    Args:
        db_url: Database connection URL; uses default when empty.

    Returns:
        AsyncEngine instance.
    """
    global _engine
    url = db_url or DEFAULT_DB_URL
    if _engine is None or str(_engine.url) != url:
        kwargs: dict[str, Any] = {"echo": False}
        if "sqlite" in url:
            # SQLite-specific settings
            kwargs["connect_args"] = {"check_same_thread": False}
        elif "postgresql" in url:
            # PostgreSQL connection pool configuration
            kwargs["pool_size"] = 10
            kwargs["max_overflow"] = 20
            kwargs["pool_pre_ping"] = True
            kwargs["pool_recycle"] = 3600
        _engine = create_async_engine(url, **kwargs)
    return _engine


def get_session_factory(
    engine: AsyncEngine | None = None,
) -> async_sessionmaker[AsyncSession]:
    """Get or create an async session factory.

    Args:
        engine: Optional engine instance; uses default engine when empty.

    Returns:
        async_sessionmaker instance.
    """
    global _session_factory
    eng = engine or get_engine()
    # Rebuild session factory when engine changes, ensuring test isolation
    if _session_factory is None:
        _session_factory = async_sessionmaker(eng, expire_on_commit=False)
    return _session_factory


@asynccontextmanager
async def get_session(
    db_url: str | None = None,
) -> AsyncGenerator[AsyncSession, None]:
    """Context manager for obtaining an async database session.

    Usage:
        async with get_session() as session:
            result = await session.execute(...)

    Args:
        db_url: Optional database URL.

    Yields:
        AsyncSession instance.
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
    """Initialize the database and create all tables.

    If using SQLite and the parent directory of the database file does not exist,
    the directory is created automatically.

    Args:
        db_url: Database connection URL.
    """
    url = db_url or DEFAULT_DB_URL

    # Ensure the directory for the SQLite database file exists
    if "sqlite" in url:
        # Extract file path from URL: sqlite+aiosqlite:///path/to/db
        db_path_str = url.split("///", 1)[-1] if "///" in url else ""
        if db_path_str:
            db_path = Path(db_path_str)
            db_path.parent.mkdir(parents=True, exist_ok=True)

    engine = get_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def close_db() -> None:
    """Close database connections and release resources."""
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _session_factory = None
