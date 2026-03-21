"""AI Team OS — Project context middleware.

Extracts project directory from X-Project-Dir HTTP header,
computes project_id, and sets per-request ContextVars for DB routing.
"""

from __future__ import annotations

import hashlib
import logging
from contextvars import ContextVar
from pathlib import Path

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

logger = logging.getLogger(__name__)

# ContextVars — set per-request by middleware, read by deps.get_repository()
current_project_id: ContextVar[str] = ContextVar("current_project_id", default="")
current_db_url: ContextVar[str] = ContextVar("current_db_url", default="")


def compute_project_id(project_dir: str) -> str:
    """Compute a stable project_id from a project directory path.

    Uses MD5 hash of the normalized absolute path, truncated to 12 hex chars.

    Args:
        project_dir: Absolute path to the project directory.

    Returns:
        12-character hex string.
    """
    normalized = str(Path(project_dir).resolve())
    return hashlib.md5(normalized.encode()).hexdigest()[:12]


def get_project_db_url(project_id: str) -> str:
    """Build a SQLite database URL for a project.

    Creates the directory if it doesn't exist.

    Args:
        project_id: 12-character hex project identifier.

    Returns:
        SQLAlchemy async SQLite URL.
    """
    data_dir = Path.home() / ".claude" / "data" / "ai-team-os" / "projects" / project_id
    data_dir.mkdir(parents=True, exist_ok=True)
    return f"sqlite+aiosqlite:///{data_dir / 'data.db'}"


# Track which project DBs have been initialized (tables created + migrations run)
_initialized_dbs: set[str] = set()


async def _ensure_project_db_ready(db_url: str) -> None:
    """Initialize a per-project database on first access.

    Creates tables and runs migrations. Skips if already initialized.
    """
    if db_url in _initialized_dbs:
        return

    from aiteam.storage.connection import init_db

    await init_db(db_url)

    # Run migrations for the project DB
    from aiteam.api.deps import _run_migrations

    await _run_migrations(db_url)

    _initialized_dbs.add(db_url)
    logger.info("Initialized project DB: %s", db_url.split("///")[-1] if "///" in db_url else db_url)


class ProjectContextMiddleware(BaseHTTPMiddleware):
    """Middleware that extracts project context from request headers.

    When X-Project-Dir header is present, computes the project_id and
    sets ContextVars so downstream code (deps.py) can route to the
    correct per-project database. Also ensures the project DB is
    initialized (tables + migrations) on first access.

    When the header is absent, ContextVars remain at their defaults
    (empty string), causing fallback to the default global database.
    """

    async def dispatch(self, request: Request, call_next: object) -> Response:
        project_dir = request.headers.get("X-Project-Dir", "")

        if project_dir:
            pid = compute_project_id(project_dir)
            db_url = get_project_db_url(pid)
            current_project_id.set(pid)
            current_db_url.set(db_url)

            # Ensure project DB is initialized on first access
            await _ensure_project_db_ready(db_url)
        else:
            # Fallback: use default DB (backward compatible)
            current_project_id.set("")
            current_db_url.set("")

        response = await call_next(request)  # type: ignore[operator]
        return response
