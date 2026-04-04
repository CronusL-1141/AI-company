"""Loop (company loop) MCP tools — continuous work mode management."""

from __future__ import annotations

from typing import Any

from aiteam.mcp._base import _api_call


def register(mcp):
    """Register all loop-related MCP tools on the given FastMCP instance."""

    @mcp.tool()
    def loop_start(team_id: str) -> dict[str, Any]:
        """Start the company loop — Leader continuous work mode.

        After starting, continuously picks up highest-priority tasks. Triggers review discussion every N tasks.
        When tasks are insufficient, organize meetings to discuss direction; don't create busywork.

        Tip: Use /continuous-mode to get the full continuous work protocol,
        including loop pickup, pause/resume, member management, and detailed behavioral guidelines.

        Args:
            team_id: Team ID or name

        Returns:
            Loop status info including current phase and cycle count
        """
        result = _api_call("POST", f"/api/teams/{team_id}/loop/start")
        return result

    @mcp.tool()
    def loop_status(team_id: str) -> dict[str, Any]:
        """View current company loop status — phase, cycle, completed task count.

        Args:
            team_id: Team ID or name

        Returns:
            Loop status details including phase / current_cycle / completed_tasks_count
        """
        return _api_call("GET", f"/api/teams/{team_id}/loop/status")

    @mcp.tool()
    def loop_next_task(team_id: str, agent_id: str = "") -> dict[str, Any]:
        """Get the next task to execute — sorted by priority x time horizon x readiness.

        Pinned and critical tasks are picked up first. short > mid > long priority.
        BLOCKED tasks auto-unlock when dependencies complete; no manual handling needed.

        Args:
            team_id: Team ID or name
            agent_id: Specify Agent ID to prioritize tasks assigned to that Agent (optional)

        Returns:
            Next pending task info; empty when no tasks available
        """
        payload: dict[str, Any] = {}
        if agent_id:
            payload["agent_id"] = agent_id
        result = _api_call("POST", f"/api/teams/{team_id}/loop/next-task", payload)
        return result

    @mcp.tool()
    def loop_advance(team_id: str, trigger: str) -> dict[str, Any]:
        """Advance the loop to the next phase.

        Available triggers:
        - tasks_planned: Planning done -> Execute
        - batch_completed: A batch of tasks completed -> Monitor
        - all_tasks_done: All completed -> Review
        - issues_found: Issues found -> Return to Execute
        - all_clear: All clear -> Review
        - new_tasks_added: New tasks added -> Re-plan
        - no_more_tasks: No more tasks -> Idle

        Args:
            team_id: Team ID or name
            trigger: Trigger name

        Returns:
            Updated loop status
        """
        return _api_call("POST", f"/api/teams/{team_id}/loop/advance", {"trigger": trigger})

    @mcp.tool()
    def loop_pause(team_id: str) -> dict[str, Any]:
        """Pause the loop — preserve current state, can be resumed at any time.

        Args:
            team_id: Team ID or name

        Returns:
            Loop status after pausing
        """
        return _api_call("POST", f"/api/teams/{team_id}/loop/pause")

    @mcp.tool()
    def loop_resume(team_id: str) -> dict[str, Any]:
        """Resume the loop — continue from where it was paused.

        Args:
            team_id: Team ID or name

        Returns:
            Loop status after resuming
        """
        return _api_call("POST", f"/api/teams/{team_id}/loop/resume")

    @mcp.tool()
    def loop_review(team_id: str) -> dict[str, Any]:
        """Trigger a company loop review — auto-create a review meeting and generate statistics report.

        The review meeting contains: summary of tasks completed this cycle, failed task analysis, and next-step suggestions.
        Leader and team can discuss and produce new to-do tasks in the meeting.

        Args:
            team_id: Team ID or name

        Returns:
            Review meeting info including meeting_id / stats / topic
        """
        return _api_call("POST", f"/api/teams/{team_id}/loop/review")
