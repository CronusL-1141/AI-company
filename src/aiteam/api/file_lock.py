"""File lock manager for AI Team OS workspace isolation.

Provides a lightweight, file-system based lock mechanism so that concurrent
AI agents can declare exclusive intent to edit a file and detect conflicts
before they happen.

Storage: ~/.claude/data/ai-team-os/file-locks.json
  {
    "<normalized_path>": {
        "agent": "prompt-dev",
        "acquired_at": 1712345678.0,
        "ttl": 300
    },
    ...
  }

Design decisions:
- No database required — plain JSON on disk, readable by hooks and MCP tools.
- TTL prevents dead locks when an agent crashes without releasing.
- Path normalization (lowercase forward-slashes) prevents duplicate entries.
- All mutations use atomic write (temp file + rename) to avoid corruption.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

_LOCKS_DIR = Path(os.path.expanduser("~")) / ".claude" / "data" / "ai-team-os"
_LOCKS_FILE = _LOCKS_DIR / "file-locks.json"

DEFAULT_TTL = 300  # 5 minutes


# ============================================================
# Internal helpers
# ============================================================


def _normalize_path(file_path: str) -> str:
    """Normalize a path to a consistent key: absolute, lowercase, forward slashes."""
    return str(Path(file_path).resolve()).replace("\\", "/").lower()


def _load_locks() -> dict[str, Any]:
    """Load locks from disk, returning empty dict on any error."""
    try:
        with open(_LOCKS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_locks(locks: dict[str, Any]) -> None:
    """Atomically write locks to disk (temp + rename)."""
    _LOCKS_DIR.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=str(_LOCKS_DIR), suffix=".json.tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(locks, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, str(_LOCKS_FILE))
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _prune_expired(locks: dict[str, Any]) -> dict[str, Any]:
    """Remove expired entries and return a cleaned copy."""
    now = time.time()
    return {
        path: info
        for path, info in locks.items()
        if (now - info.get("acquired_at", 0)) < info.get("ttl", DEFAULT_TTL)
    }


# ============================================================
# Public API
# ============================================================


def acquire_lock(file_path: str, agent_name: str, ttl: int = DEFAULT_TTL) -> dict[str, Any]:
    """Try to acquire a lock on *file_path* for *agent_name*.

    Returns:
        {"success": True, "path": ..., "agent": ...}          on success
        {"success": False, "held_by": ..., "expires_in": ...}  on conflict
    """
    key = _normalize_path(file_path)
    locks = _prune_expired(_load_locks())
    now = time.time()

    existing = locks.get(key)
    if existing:
        remaining = existing.get("ttl", DEFAULT_TTL) - (now - existing.get("acquired_at", 0))
        return {
            "success": False,
            "path": key,
            "held_by": existing.get("agent", "unknown"),
            "expires_in": max(0.0, round(remaining, 1)),
            "message": (
                f"文件 {key} 已被 {existing.get('agent', 'unknown')} 锁定，"
                f"剩余 {max(0, int(remaining))} 秒。请等待锁释放后再编辑。"
            ),
        }

    locks[key] = {
        "agent": agent_name,
        "acquired_at": now,
        "ttl": ttl,
    }
    _save_locks(locks)
    return {
        "success": True,
        "path": key,
        "agent": agent_name,
        "ttl": ttl,
        "message": f"文件锁已获取: {key}（TTL {ttl}秒）",
    }


def release_lock(file_path: str, agent_name: str) -> dict[str, Any]:
    """Release a lock held by *agent_name* on *file_path*.

    Returns:
        {"success": True}  on release
        {"success": False, reason: ...}  when no matching lock found
    """
    key = _normalize_path(file_path)
    locks = _prune_expired(_load_locks())

    existing = locks.get(key)
    if not existing:
        return {"success": False, "path": key, "message": f"文件 {key} 没有活跃锁"}

    if existing.get("agent") != agent_name:
        return {
            "success": False,
            "path": key,
            "message": (
                f"文件 {key} 由 {existing.get('agent')} 持有，"
                f"{agent_name} 无法释放他人的锁"
            ),
        }

    del locks[key]
    _save_locks(locks)
    return {"success": True, "path": key, "agent": agent_name, "message": f"文件锁已释放: {key}"}


def check_lock(file_path: str) -> dict[str, Any]:
    """Check whether *file_path* is currently locked.

    Returns:
        {"locked": False}                         when free
        {"locked": True, "held_by": ..., ...}     when locked
    """
    key = _normalize_path(file_path)
    locks = _prune_expired(_load_locks())

    info = locks.get(key)
    if not info:
        return {"locked": False, "path": key}

    now = time.time()
    remaining = info.get("ttl", DEFAULT_TTL) - (now - info.get("acquired_at", 0))
    return {
        "locked": True,
        "path": key,
        "held_by": info.get("agent", "unknown"),
        "expires_in": round(max(0.0, remaining), 1),
    }


def list_locks() -> dict[str, Any]:
    """Return all active (non-expired) locks.

    Returns:
        {"locks": [{path, agent, expires_in}, ...], "count": N}
    """
    locks = _prune_expired(_load_locks())
    now = time.time()
    result = []
    for path, info in locks.items():
        remaining = info.get("ttl", DEFAULT_TTL) - (now - info.get("acquired_at", 0))
        result.append(
            {
                "path": path,
                "agent": info.get("agent", "unknown"),
                "expires_in": round(max(0.0, remaining), 1),
            }
        )
    result.sort(key=lambda x: x["path"])
    return {"locks": result, "count": len(result)}
