"""Task management MCP tools."""

from __future__ import annotations

import urllib.parse
from typing import Any

from aiteam.mcp._base import _api_call, _resolve_project_id, _resolve_team_id


def register(mcp):
    """Register all task-related MCP tools."""

    @mcp.tool()
    def task_run(
        team_id: str,
        description: str,
        title: str = "",
        model: str | None = None,
        depends_on: list[str] | None = None,
    ) -> dict[str, Any]:
        """Create a task in a team, waiting for an Agent to pick up and execute.

        Rule: Set priority (critical/high/medium/low) and horizon (short/mid/long).
        Use depends_on for dependencies; the system auto-manages BLOCKED status.
        Coordinate parallel execution — don't wait for one to complete before starting the next.

        Args:
            team_id: Team ID or name
            description: Task description
            title: Task title (optional)
            model: Specify model to use (optional, metadata only)
            depends_on: List of dependency task IDs (optional, task auto-unlocks when dependencies complete)

        Returns:
            Created task info + related_tasks (similar tasks list, if any)
        """
        payload: dict[str, Any] = {"description": description}
        if title:
            payload["title"] = title
        if model:
            payload["model"] = model
        if depends_on:
            payload["depends_on"] = depends_on
        result = _api_call("POST", f"/api/teams/{team_id}/tasks/run", payload)
        return result

    @mcp.tool()
    def task_decompose(
        team_id: str,
        title: str,
        description: str = "",
        template: str = "",
        subtasks: list[dict[str, str]] | None = None,
        auto_assign: bool = False,
    ) -> dict[str, Any]:
        """Decompose a large task into a parent task + subtasks.

        Supports two approaches:
        1. Use a built-in template to auto-generate subtasks
        2. Manually specify a subtask list

        Available templates: web-app, api-service, data-pipeline, library, refactor, bugfix

        Args:
            team_id: Team ID or name
            title: Parent task title
            description: Parent task description
            template: Built-in template name (optional)
            subtasks: Custom subtask list, each with title and optional description (optional)
            auto_assign: Whether to auto-assign to matching-role Agents (not yet implemented)

        Returns:
            Parent task + subtask list
        """
        payload: dict[str, Any] = {
            "title": title,
            "description": description,
            "template": template,
            "auto_assign": auto_assign,
        }
        if subtasks:
            payload["subtasks"] = subtasks
        return _api_call("POST", f"/api/teams/{team_id}/tasks/decompose", payload)

    @mcp.tool()
    def task_create(
        title: str,
        project_id: str = "",
        description: str = "",
        priority: str = "medium",
        horizon: str = "mid",
        tags: list[str] | None = None,
        auto_start: bool = False,
        task_type: str = "",
    ) -> dict[str, Any]:
        """Create a new task in a project (not bound to a team).

        Project-level tasks are attached directly to the project and visible
        on the project task wall. Suitable for planning-phase tasks not yet assigned to a team.

        Args:
            title: Task title
            project_id: Project ID (optional, auto-uses active project if empty)
            description: Task description
            priority: Priority, one of "critical" / "high" / "medium" / "low"
            horizon: Time horizon, one of "short" / "mid" / "long"
            tags: Tag list
            auto_start: If True, immediately set status to 'running' after creation
            task_type: Optional workflow pipeline type to auto-attach after creation.
                One of: feature / bugfix / research / refactor / quick-fix / spike / hotfix.
                Lightest option: quick-fix (Implement → Test only).

        Returns:
            Created task info, with pipeline info included when task_type is provided
        """
        resolved = _resolve_project_id(project_id)
        if not resolved:
            return {"success": False, "error": "未找到活跃项目，请提供 project_id 或先创建项目"}
        payload: dict[str, Any] = {
            "title": title,
            "description": description,
            "priority": priority,
            "horizon": horizon,
        }
        if tags:
            payload["tags"] = tags
        result = _api_call("POST", f"/api/projects/{resolved}/tasks", payload)
        if auto_start and result.get("success") and result.get("data", {}).get("id"):
            task_id = result["data"]["id"]
            _api_call("PUT", f"/api/tasks/{task_id}", {"status": "running"})
            result["data"]["status"] = "running"
            result["message"] = "任务已创建并开始执行"
        _valid_task_types = {"feature", "bugfix", "research", "refactor", "quick-fix", "spike", "hotfix"}
        if task_type and task_type in _valid_task_types and result.get("success"):
            created_task_id = result.get("data", {}).get("id")
            if created_task_id:
                pipeline_result = _api_call(
                    "POST",
                    f"/api/tasks/{created_task_id}/pipeline",
                    {"pipeline_type": task_type},
                )
                result["pipeline"] = pipeline_result.get("data") or pipeline_result
                if pipeline_result.get("success"):
                    result["message"] = (
                        result.get("message", "任务已创建")
                        + f"，已自动挂载 {task_type} 工作流管道"
                    )
        return result

    @mcp.tool()
    def task_status(task_id: str) -> dict[str, Any]:
        """Query the current status of a task.

        Args:
            task_id: Task ID

        Returns:
            Task details including status, result, etc.
        """
        return _api_call("GET", f"/api/tasks/{task_id}")

    @mcp.tool()
    def task_update(
        task_id: str,
        status: str = "",
        assigned_to: str = "",
        result: str = "",
        priority: str = "",
        tags: list[str] | None = None,
        title: str = "",
        description: str = "",
    ) -> dict[str, Any]:
        """Update a task's fields (partial update — only provided fields are changed).

        Status transitions automatically set timestamps:
          - running  → started_at = now
          - completed → completed_at = now

        Args:
            task_id: Task ID (required)
            status: New status: pending / blocked / running / completed / failed
            assigned_to: Agent name or ID to assign the task to
            result: Task result text (typically filled when completing)
            priority: Priority: critical / high / medium / low
            tags: New tag list (replaces existing tags)
            title: New task title
            description: New task description

        Returns:
            Updated task data
        """
        payload: dict[str, Any] = {}
        if status:
            payload["status"] = status
        if assigned_to:
            payload["assigned_to"] = assigned_to
        if result:
            payload["result"] = result
        if priority:
            payload["priority"] = priority
        if tags is not None:
            payload["tags"] = tags
        if title:
            payload["title"] = title
        if description:
            payload["description"] = description
        return _api_call("PUT", f"/api/tasks/{task_id}", payload)

    @mcp.tool()
    def task_auto_match(team_id: str) -> dict[str, Any]:
        """Get intelligent task-Agent matching suggestions.

        Analyzes the match between pending unassigned tasks and idle/offline Agents
        in the team, returning recommended assignments sorted by match_score.

        Args:
            team_id: Team ID or name

        Returns:
            Matching suggestions list, each containing task_id, task_title, agent_id, agent_name, match_score
        """
        return _api_call("GET", f"/api/teams/{team_id}/task-matches")

    @mcp.tool()
    def task_subtasks(task_id: str) -> dict[str, Any]:
        """List subtasks of a parent task.

        Args:
            task_id: Parent task ID

        Returns:
            List of subtasks with status
        """
        return _api_call("GET", f"/api/tasks/{task_id}/subtasks")

    @mcp.tool()
    def taskwall_view(
        team_id: str,
        horizon: str = "",
        priority: str = "",
    ) -> dict[str, Any]:
        """Get the task wall view — categorized by short/mid/long term with intelligent sorting.

        Returns a task list sorted by score, helping Leader quickly understand what to do next.

        Args:
            team_id: Team ID or name
            horizon: Filter by time horizon, one of "short" / "mid" / "long" (empty = all)
            priority: Filter by priority, one of "critical" / "high" / "medium" / "low",
                comma-separated for multiple (empty = all)

        Returns:
            Task wall data grouped by short/mid/long, each group sorted by score descending
        """
        params: list[str] = []
        if horizon:
            params.append(f"horizon={urllib.parse.quote(horizon)}")
        if priority:
            params.append(f"priority={urllib.parse.quote(priority)}")
        qs = f"?{'&'.join(params)}" if params else ""
        return _api_call("GET", f"/api/teams/{team_id}/task-wall{qs}")

    @mcp.tool()
    def task_list_project(
        project_id: str = "",
        horizon: str = "",
        priority: str = "",
        limit: int = 50,
        offset: int = 0,
        include_completed: bool = False,
        status: str = "",
    ) -> dict[str, Any]:
        """Get project-level task wall — tasks belonging to a project (across all teams).

        Unlike taskwall_view (which is team-scoped), this returns tasks from all teams
        under a project plus standalone project-level tasks.

        Args:
            project_id: Project ID (optional, auto-uses active project if empty)
            horizon: Filter by time horizon: "short" / "mid" / "long" (optional)
            priority: Filter by priority: "critical" / "high" / "medium" / "low" (optional)
            limit: Max number of active tasks to return (default 50)
            offset: Pagination offset for active tasks (default 0)
            include_completed: Include completed tasks in response (default False)
            status: Filter by status: pending/running/blocked/completed (default all active)

        Returns:
            Project task wall with wall (grouped by horizon), completed tasks, and stats
        """
        resolved = _resolve_project_id(project_id)
        if not resolved:
            return {"success": False, "error": "未找到活跃项目，请提供 project_id 或先创建项目"}
        params: list[str] = [
            f"limit={limit}",
            f"offset={offset}",
            f"include_completed={'true' if include_completed else 'false'}",
        ]
        if horizon:
            params.append(f"horizon={urllib.parse.quote(horizon)}")
        if priority:
            params.append(f"priority={urllib.parse.quote(priority)}")
        if status:
            params.append(f"status={urllib.parse.quote(status)}")
        qs = f"?{'&'.join(params)}"
        return _api_call("GET", f"/api/projects/{resolved}/task-wall{qs}")

    @mcp.tool()
    def task_memo_read(task_id: str) -> dict[str, Any]:
        """Read all memo records for a task — read before picking up a task to understand historical progress.

        Args:
            task_id: Task ID

        Returns:
            Memo record list in chronological order
        """
        return _api_call("GET", f"/api/tasks/{task_id}/memo")

    @mcp.tool()
    def task_memo_add(
        task_id: str,
        content: str,
        memo_type: str = "progress",
        author: str = "leader",
    ) -> dict[str, Any]:
        """Add a memo record to a task — for tracking progress, recording decisions, marking issues.

        Args:
            task_id: Task ID
            content: Memo content
            memo_type: Type, one of "progress" / "decision" / "issue" / "summary"
            author: Author name, default "leader"

        Returns:
            Added memo record
        """
        return _api_call(
            "POST",
            f"/api/tasks/{task_id}/memo",
            {
                "content": content,
                "type": memo_type,
                "author": author,
            },
        )

    @mcp.tool()
    def task_execution_trace(task_id: str) -> dict[str, Any]:
        """Get complete execution timeline for a task.

        Returns a unified chronological timeline of all memo records and task
        lifecycle events, showing who did what, when, and with what result.

        Args:
            task_id: Task ID

        Returns:
            task: Task details
            timeline: Chronologically sorted list of events and memo records
            total_events: Total event count
        """
        return _api_call("GET", f"/api/tasks/{task_id}/execution-trace")
