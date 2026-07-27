"""Task analysis MCP tools — failure alchemy + failure diagnosis."""

from __future__ import annotations

from typing import Any

from aiteam.mcp._base import _api_call


def register(mcp):
    """Register all task-analysis MCP tools."""

    @mcp.tool()
    def failure_analysis(task_id: str, team_id: str) -> dict[str, Any]:
        """Analyze failed tasks, distill defense rules + training cases + improvement proposals (failure alchemy).

        When a task permanently fails (exceeds retry limit), call this tool for deep failure analysis.
        Automatically generates three learning artifacts saved to team memory:
        - Antibody: Defensive rule suggestions to prevent similar failures
        - Vaccine: Structured failure case for new Agents to reference and learn from
        - Catalyst: System improvement proposals to drive process optimization

        Args:
            task_id: ID of the failed task
            team_id: ID of the owning team

        Returns:
            Dict containing antibody, vaccine, and catalyst artifacts
        """
        return _api_call("POST", f"/api/teams/{team_id}/failure-analysis", {"task_id": task_id})

    @mcp.tool()
    def diagnose_task_failure(task_id: str) -> dict[str, Any]:
        """Auto-diagnose why a task failed and suggest fixes.

        Reads the task's execution trace (memos) to identify the failure point,
        compares with similar successful tasks in the same team, and returns
        actionable fix suggestions.

        Use this when a task fails or gets stuck to quickly understand root cause
        without manually reading through all memo records.

        Args:
            task_id: ID of the failed or stuck task

        Returns:
            Dict with root_cause, failed_at, similar_successes count,
            suggested_fixes list, and rollback_recommendation
        """
        return _api_call("POST", f"/api/tasks/{task_id}/diagnose", {})
