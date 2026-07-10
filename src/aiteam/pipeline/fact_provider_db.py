"""Production FactProvider — queries Repository + filesystem for objective facts."""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime

logger = logging.getLogger(__name__)

_MTIME_TOLERANCE_SECONDS = 2  # Council R1 Issue 7


def _ensure_aware(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


class DbFactProvider:
    """Production FactProvider that walks src/ and queries the repository."""

    def __init__(self, repo: object, project_root: str) -> None:
        self._repo = repo
        self._project_root = project_root

    # ------------------------------------------------------------------
    # Subtask count
    # ------------------------------------------------------------------

    async def count_subtasks(self, parent_id: str) -> int:
        """Return count of child tasks."""
        try:
            subtasks = await self._repo.list_subtasks(parent_id)
            return len(subtasks)
        except Exception as exc:
            logger.warning("DbFactProvider.count_subtasks failed: %s", exc)
            return 0

    # ------------------------------------------------------------------
    # File modification check
    # ------------------------------------------------------------------

    def src_files_modified_since(self, since: datetime) -> bool:
        """Walk project_root/src/ and return True if any .py file has mtime > since.

        Uses 2-second tolerance guard (Council R1 Issue 7) — callers should
        already have added tolerance to `since` before calling this method.
        """
        since_aware = _ensure_aware(since)
        src_root = os.path.join(self._project_root, "src")
        if not os.path.isdir(src_root):
            logger.debug("DbFactProvider: src/ not found at %s", src_root)
            return False

        for dirpath, _dirnames, filenames in os.walk(src_root):
            for fname in filenames:
                if not fname.endswith(".py"):
                    continue
                fpath = os.path.join(dirpath, fname)
                try:
                    mtime = os.path.getmtime(fpath)
                    mtime_dt = datetime.fromtimestamp(mtime, tz=UTC)
                    if mtime_dt > since_aware:
                        return True
                except OSError:
                    continue
        return False

    # ------------------------------------------------------------------
    # Last Bash event
    # ------------------------------------------------------------------

    async def last_bash_event(self, task_id: str) -> dict | None:
        """Query the most recent PostToolUse Bash activity for task_id.

        Returns dict with keys: exit_code (int), stdout (str).
        Falls back to task.config["last_bash"] if no agent_activity record found.
        """
        try:
            task = await self._repo.get_task(task_id)
            if task is None:
                return None
            config = task.config or {}
            bash_info = config.get("last_bash")
            if bash_info and isinstance(bash_info, dict):
                return bash_info
        except Exception as exc:
            logger.warning("DbFactProvider.last_bash_event failed: %s", exc)
        return None

    # ------------------------------------------------------------------
    # Memos since
    # ------------------------------------------------------------------

    async def memos_since(
        self, task_id: str, since: datetime, memo_type: str | None = None
    ) -> list[dict]:
        """Return memo records from task.config['memo'] added after since."""
        from datetime import datetime as dt_cls

        since_aware = _ensure_aware(since)
        try:
            task = await self._repo.get_task(task_id)
            if task is None:
                return []
            memos: list[dict] = (task.config or {}).get("memo", [])
            result = []
            for memo in memos:
                ts_raw = memo.get("timestamp")
                if not ts_raw:
                    continue
                try:
                    ts = dt_cls.fromisoformat(str(ts_raw))
                    ts = _ensure_aware(ts)
                except ValueError:
                    continue
                if ts <= since_aware:
                    continue
                if memo_type is not None and memo.get("type") != memo_type:
                    continue
                result.append(memo)
            return result
        except Exception as exc:
            logger.warning("DbFactProvider.memos_since failed: %s", exc)
            return []
