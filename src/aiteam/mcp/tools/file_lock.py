"""MCP tools for file lock / workspace isolation.

Exposes three tools:
  file_lock_acquire  — declare exclusive edit intent on a file
  file_lock_release  — release the lock after editing is done
  file_lock_list     — inspect all currently held locks
"""

from __future__ import annotations

from typing import Any

from aiteam.api.file_lock import (
    acquire_lock,
    check_lock,
    list_locks,
    release_lock,
)


def register(mcp) -> None:
    """Register file lock tools on the FastMCP instance."""

    @mcp.tool()
    def file_lock_acquire(file_path: str, agent_name: str, ttl: int = 300) -> dict[str, Any]:
        """Declare exclusive edit intent on a file to prevent concurrent modifications.

        Call this before editing a shared file (types.py, models.py, etc.).
        If another agent already holds the lock, the call fails with details
        about who holds it and how long until it expires.

        Args:
            file_path: Absolute or relative path to the file to lock
            agent_name: Your agent name (e.g. "prompt-dev", "event-dev")
            ttl: Lock lifetime in seconds (default 300 = 5 minutes). Auto-expires
                 to prevent dead locks if you crash without calling file_lock_release.

        Returns:
            On success: {"success": True, "path": ..., "agent": ..., "ttl": ...}
            On conflict: {"success": False, "held_by": ..., "expires_in": ..., "message": ...}
        """
        return acquire_lock(file_path, agent_name, ttl)

    @mcp.tool()
    def file_lock_release(file_path: str, agent_name: str) -> dict[str, Any]:
        """Release the file lock after editing is complete.

        Call this immediately after you finish editing a file you locked with
        file_lock_acquire. This allows other agents to proceed without waiting
        for the TTL to expire.

        Args:
            file_path: Path of the file to unlock (same value used in acquire)
            agent_name: Your agent name — must match the lock owner

        Returns:
            {"success": True}  or  {"success": False, "message": ...}
        """
        return release_lock(file_path, agent_name)

    @mcp.tool()
    def file_lock_list() -> dict[str, Any]:
        """List all currently active file locks held by agents.

        Useful for team-lead to inspect the workspace state or diagnose
        potential conflicts before dispatching concurrent agents.

        Returns:
            {"locks": [{"path": ..., "agent": ..., "expires_in": ...}, ...], "count": N}
        """
        return list_locks()

    @mcp.tool()
    def file_lock_check(file_path: str) -> dict[str, Any]:
        """Check whether a specific file is currently locked by any agent.

        Args:
            file_path: Path to the file to check

        Returns:
            {"locked": False}  or  {"locked": True, "held_by": ..., "expires_in": ...}
        """
        return check_lock(file_path)
