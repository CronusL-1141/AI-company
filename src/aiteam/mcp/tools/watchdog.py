"""Watchdog completion verification MCP tools."""

from __future__ import annotations

from typing import Any

from aiteam.mcp._base import _api_call


def register(mcp):
    """Register watchdog-related MCP tools."""

    @mcp.tool()
    def verify_completion(task_id: str) -> dict[str, Any]:
        """Verify whether a task is truly complete.

        Checks:
        1. Task status == completed
        2. At least one memo record exists (task_memo_add was called)
        3. A summary-type memo exists (task_memo_add type='summary' was called)

        Use this after an agent reports completion to ensure all artifacts are present.

        Args:
            task_id: Task ID to verify

        Returns:
            Verification result with passed bool and list of issues if any
        """
        return _api_call("POST", f"/api/watchdog/verify/{task_id}", {})
