"""AI Team OS — Async database connection management.

Provides SQLAlchemy async engine and session management with automatic table creation.
Supports per-project database isolation via EnginePool.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
)

from aiteam.storage.engine_pool import engine_pool
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


def get_engine(db_url: str | None = None) -> AsyncEngine:
    """Get or create an async database engine.

    Uses the global EnginePool to support multiple concurrent databases
    (per-project isolation).

    Args:
        db_url: Database connection URL; uses default when empty.

    Returns:
        AsyncEngine instance.
    """
    url = db_url or DEFAULT_DB_URL
    return engine_pool.get_engine(url)


@asynccontextmanager
async def get_session(
    db_url: str | None = None,
) -> AsyncGenerator[AsyncSession, None]:
    """Context manager for obtaining an async database session.

    Uses the EnginePool to route to the correct database.

    Usage:
        async with get_session() as session:
            result = await session.execute(...)

    Args:
        db_url: Optional database URL.

    Yields:
        AsyncSession instance.
    """
    url = db_url or DEFAULT_DB_URL
    factory = engine_pool.get_session_factory(url)
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
    """Close all database connections and release resources."""
    await engine_pool.dispose_all()
