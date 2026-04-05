"""Agent trust scoring MCP tools."""

from __future__ import annotations

from typing import Any

from aiteam.mcp._base import _api_call


def register(mcp):
    """Register trust-scoring MCP tools."""

    @mcp.tool()
    def agent_trust_scores() -> dict[str, Any]:
        """Query Agent trust score leaderboard.

        Trust scores range from 0.0 to 1.0 (default 0.5).
        Scores increase by 0.05 on task success, decrease by 0.10 on failure,
        and decrease by 0.05 on timeout.

        Returns:
            List of all agents sorted by trust_score descending,
            each entry contains agent_id, agent_name, team_id, team_name, trust_score.
        """
        return _api_call("GET", "/api/agents/trust-scores")

    @mcp.tool()
    def agent_trust_update(agent_id: str, task_result: str) -> dict[str, Any]:
        """Update an Agent's trust score based on task outcome.

        Args:
            agent_id: Target agent ID
            task_result: One of "success" (+0.05), "failure" (-0.10), "timeout" (-0.05)

        Returns:
            Dict with agent_id, task_result, and updated trust_score.
        """
        import urllib.parse
        qs = urllib.parse.urlencode({"task_result": task_result})
        return _api_call("POST", f"/api/agents/{agent_id}/trust?{qs}")
