"""Watchdog heartbeat and completion verification MCP tools."""

from __future__ import annotations

from typing import Any

from aiteam.mcp._base import _api_call


def register(mcp):
    """Register watchdog-related MCP tools."""

    @mcp.tool()
    def agent_heartbeat(
        agent_id: str,
        agent_name: str = "",
        team_id: str = "",
    ) -> dict[str, Any]:
        """Report that an agent is still alive — call periodically to maintain liveness.

        An agent is considered dead if no heartbeat is received within 5 minutes.
        Dead agents will appear in watchdog_check() results.

        Args:
            agent_id: Unique agent identifier (use your agent name or ID)
            agent_name: Human-readable agent name (optional, for display)
            team_id: Team the agent belongs to (optional)

        Returns:
            Heartbeat record with agent_id and timestamp
        """
        return _api_call(
            "POST",
            "/api/watchdog/heartbeat",
            {"agent_id": agent_id, "agent_name": agent_name, "team_id": team_id},
        )

    @mcp.tool()
    def watchdog_check() -> dict[str, Any]:
        """Check health status of all registered agents based on heartbeat timestamps.

        An agent is considered dead if its last heartbeat is older than 5 minutes.

        Returns:
            Dict with alive/dead agent lists and summary counts
        """
        return _api_call("GET", "/api/watchdog/heartbeats")

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
