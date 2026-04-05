"""SRE Error Budget MCP tools."""

from __future__ import annotations

from typing import Any

from aiteam.mcp._base import _api_call


def register(mcp):
    """Register error budget MCP tools."""

    @mcp.tool()
    def error_budget_status(team_id: str = "global") -> dict[str, Any]:
        """View current SRE error budget status for a team.

        The error budget uses a 4-level model based on failure rate over the last 20 tasks:
          GREEN  (<15%)   — normal autonomous operation
          YELLOW (15-25%) — warning, reduce autonomy
          ORANGE (25-35%) — severe, every action needs Leader approval
          RED    (>35%)   — stopped, wait for human intervention

        Args:
            team_id: Team ID (default "global" for cross-team aggregate)

        Returns:
            Budget state with level, failure_rate, policy, and recent window stats
        """
        import urllib.parse
        params = urllib.parse.urlencode({"team_id": team_id})
        return _api_call("GET", f"/api/error-budget?{params}")

    @mcp.tool()
    def error_budget_update(
        team_id: str,
        task_success: bool,
    ) -> dict[str, Any]:
        """Update error budget after a task completes.

        Automatically called after task completion. Appends to the sliding window
        (last 20 tasks) and recomputes the autonomy level.

        Args:
            team_id: Team ID
            task_success: True if task succeeded, False if failed/blocked

        Returns:
            Updated budget state with new level and whether level changed
        """
        return _api_call(
            "POST",
            "/api/error-budget/update",
            {"team_id": team_id, "task_success": task_success},
        )
