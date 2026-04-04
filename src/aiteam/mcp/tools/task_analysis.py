"""Task analysis MCP tools — failure analysis, diagnostics, replay, comparison."""

from __future__ import annotations

import urllib.parse
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

    @mcp.tool()
    def what_if_analysis(task_id: str, team_id: str = "") -> dict[str, Any]:
        """Perform What-If analysis on a task — generate multi-approach comparison and recommendation.

        During task planning, generates 2-3 alternative approaches with quick scoring comparison:
        - Approach A: Best role-match assignment (lowest risk)
        - Approach B: Parallel split execution (faster, appears when idle agents >= 2)
        - Approach C: History-driven based on experience (appears when team has memory)

        Args:
            task_id: Task ID to analyze
            team_id: Owning team ID (optional, can be empty if task is already bound to a team)

        Returns:
            Dict containing approaches list, recommendation, and analysis time
        """
        params = f"?team_id={urllib.parse.quote(team_id)}" if team_id else ""
        return _api_call("GET", f"/api/tasks/{task_id}/what-if{params}")

    @mcp.tool()
    def task_replay(task_id: str) -> dict[str, Any]:
        """Get full execution replay for a task — timeline, checkpoints, stats.

        Returns a step-by-step replay of the task execution including:
        - timeline: all memo records and lifecycle events in chronological order
        - checkpoints: key decision/summary points only
        - stats: duration, step count, subtask count, memo type breakdown

        Use this to review how a task was executed, understand the decision trail,
        or audit agent behavior post-completion.

        Args:
            task_id: Task ID to replay

        Returns:
            Dict with task, timeline, checkpoints, and stats fields
        """
        return _api_call("GET", f"/api/tasks/{task_id}/replay")

    @mcp.tool()
    def task_compare(task_id_1: str, task_id_2: str) -> dict[str, Any]:
        """Compare two task executions side by side.

        Fetches full replay data for both tasks and produces a diff highlighting:
        - step count difference
        - checkpoint count difference
        - duration difference
        - authors unique to each execution vs shared

        Useful for comparing a failed run against a successful one, or benchmarking
        different agent assignments on the same type of task.

        Args:
            task_id_1: First task ID
            task_id_2: Second task ID

        Returns:
            Dict with task_1 replay, task_2 replay, and diff summary
        """
        params = f"?task_id_1={urllib.parse.quote(task_id_1)}&task_id_2={urllib.parse.quote(task_id_2)}"
        return _api_call("GET", f"/api/tasks/compare{params}")
