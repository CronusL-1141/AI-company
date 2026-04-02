"""AI Team OS — Project context utilities.

Provides compute_project_id() for deriving a stable project identifier
from a directory path. Used by cross-project messaging routes.

Per-project DB isolation was removed — all data lives in the default DB.
get_all_project_db_urls() is kept as a stub returning [] to avoid import errors.
"""

from __future__ import annotations

import hashlib
from pathlib import Path


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


def get_all_project_db_urls() -> list[str]:
    """Stub — per-project DB isolation was removed. Always returns empty list."""
    return []
