"""Scheduler MCP tools."""

from __future__ import annotations

import json
import re
import urllib.parse
from typing import Any

from aiteam.mcp._base import _api_call


def _parse_interval(interval: str) -> int:
    """Parse human-readable interval string to seconds.

    Examples: "2 days" -> 172800, "1 hour" -> 3600, "30 minutes" -> 1800,
              "5 min" -> 300, "1d" -> 86400, "2h" -> 7200
    """
    interval = interval.strip().lower()

    m = re.fullmatch(
        r"(\d+(?:\.\d+)?)\s*(d|day|days|h|hr|hour|hours|m|min|mins|minute|minutes|s|sec|second|seconds)", interval
    )
    if not m:
        raise ValueError(
            f"Cannot parse interval '{interval}'. Use formats like '2 days', '1 hour', '30 minutes', '300 seconds'."
        )
    value = float(m.group(1))
    unit = m.group(2)
    if unit in ("d", "day", "days"):
        return int(value * 86400)
    elif unit in ("h", "hr", "hour", "hours"):
        return int(value * 3600)
    elif unit in ("m", "min", "mins", "minute", "minutes"):
        return int(value * 60)
    else:
        return int(value)


def register(mcp):
    """Register all scheduler-related MCP tools."""

    @mcp.tool()
    def scheduler_create(
        name: str,
        interval: str,
        action_type: str,
        action_config: str = "{}",
        team_id: str = "",
        description: str = "",
    ) -> dict[str, Any]:
        """Create a scheduled task that triggers automatically on a fixed interval.

        Args:
            name: Task name (unique identifier)
            interval: Human-readable interval, e.g. "2 days", "1 hour", "30 minutes" (minimum 5 minutes)
            action_type: One of "create_task" / "inject_reminder" / "emit_event"
            action_config: JSON string with action parameters.
                - create_task: {"title": "...", "description": "...", "priority": "medium"}
                - inject_reminder: {"message": "..."}
                - emit_event: {"event_type": "...", "data": {...}}
            team_id: Team ID to scope this task (optional)
            description: Human-readable description

        Returns:
            Created scheduled task info
        """
        try:
            interval_seconds = _parse_interval(interval)
        except ValueError as e:
            return {"success": False, "error": str(e)}

        if interval_seconds < 300:
            return {
                "success": False,
                "error": f"Interval too short ({interval_seconds}s). Minimum is 300s (5 minutes).",
            }

        try:
            config = json.loads(action_config) if action_config else {}
        except json.JSONDecodeError as e:
            return {"success": False, "error": f"Invalid action_config JSON: {e}"}

        payload: dict[str, Any] = {
            "name": name,
            "interval_seconds": interval_seconds,
            "action_type": action_type,
            "action_config": config,
            "description": description,
        }
        if team_id:
            payload["team_id"] = team_id

        return _api_call("POST", "/api/scheduler", payload)

    @mcp.tool()
    def scheduler_list(team_id: str = "") -> dict[str, Any]:
        """List all scheduled tasks, optionally filtered by team.

        Args:
            team_id: Filter by team ID (optional, empty = list all)

        Returns:
            List of scheduled tasks with status and next_run_at
        """
        path = "/api/scheduler"
        if team_id:
            path += f"?team_id={urllib.parse.quote(team_id)}"
        return _api_call("GET", path)

    @mcp.tool()
    def scheduler_pause(task_id: str) -> dict[str, Any]:
        """Pause a scheduled task (set enabled=False).

        Args:
            task_id: Scheduled task ID

        Returns:
            Updated task info
        """
        return _api_call("PUT", f"/api/scheduler/{task_id}", {"enabled": False})

    @mcp.tool()
    def scheduler_delete(task_id: str) -> dict[str, Any]:
        """Permanently delete a scheduled task.

        Args:
            task_id: Scheduled task ID

        Returns:
            Deletion result
        """
        return _api_call("DELETE", f"/api/scheduler/{task_id}")
