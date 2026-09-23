"""Task analysis MCP tools — failure alchemy + failure diagnosis."""

from __future__ import annotations

from typing import Any

from aiteam.mcp._base import _api_call


def register(mcp):
    """Register all task-analysis MCP tools."""

    @mcp.tool()
    def failure_analysis(task_id: str, team_id: str) -> dict[str, Any]:
        """Record a failed task as a templated lesson entry (failure alchemy).

        Frozen: still callable, no longer developed.

        The watchdog runs this itself once it stops retrying a failed task; call it
        by hand only on a failed task that will not be retried. The three
        artifacts are fixed templates filled from the task's title, result,
        recorded error, and tags; nothing is inferred beyond those fields, so
        the output is only as specific as the task's recorded result and error.
        For a diagnosis of why a task failed, use diagnose_task_failure.
        - Antibody: defensive-rule suggestion built from the failure reason
        - Vaccine: failure case (description, assignee, result, prevention)
        - Catalyst: improvement proposal keyed on the task's tags
        The combined text is appended to the task as an issue memo
        (task_memo_read shows it).

        Args:
            task_id: ID of the failed task
            team_id: ID of the owning team

        Returns:
            Dict containing antibody, vaccine, and catalyst artifacts and memo_id
        """
        return _api_call("POST", f"/api/teams/{team_id}/failure-analysis", {"task_id": task_id})

    @mcp.tool()
    def diagnose_task_failure(task_id: str) -> dict[str, Any]:
        """Auto-diagnose why a task failed and suggest fixes.

        Frozen: still callable, no longer developed.

        Reads the task's valid memos to identify the failure point, compares
        with similar successful tasks in the same team, and returns actionable
        fix suggestions. Each call records a task.failure_diagnosed event.

        Use this when a task fails or gets stuck to quickly understand root cause
        without manually reading through all memo records.

        Args:
            task_id: ID of the failed or stuck task

        Returns:
            Dict with root_cause, failed_at, similar_successes count,
            suggested_fixes list, and rollback_recommendation
        """
        return _api_call("POST", f"/api/tasks/{task_id}/diagnose", {})
