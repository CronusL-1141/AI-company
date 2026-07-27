"""Task management MCP tools."""

from __future__ import annotations

import urllib.parse
from typing import Any

from aiteam.mcp._base import _api_call, _resolve_project_id
from aiteam.mcp.tools.views import (
    FIELDS_ERROR,
    TASK_WALL_HINT,
    compact_task_row,
    resolve_view,
)


def register(mcp):
    """Register all task-related MCP tools."""

    @mcp.tool()
    def task_run(
        team_id: str,
        description: str,
        title: str = "",
        model: str | None = None,
        depends_on: list[str] | None = None,
        priority: str = "",
        horizon: str = "",
        tags: list[str] | None = None,
        assigned_to: str = "",
    ) -> dict[str, Any]:
        """Put a task on a team's wall. Nothing executes it — an Agent has to pick it up.

        The name is historical: there was once a worker pool that would "run" the
        task. That pool is retired; this tool only creates the row. Dispatch is
        yours to do (Agent(...) / SendMessage), and the sub-agent then writes
        progress back with task_memo_add.

        Priority and horizon drive the task wall's ordering, so set them here —
        the old docstring told callers to "set priority and horizon" while the
        signature had no such parameters, and any value passed was silently
        dropped (fixed 2026-07-27).

        Args:
            team_id: Team ID or name
            description: Task description
            title: Task title (optional)
            model: Specify model to use (optional, metadata only)
            depends_on: Dependency task IDs — task auto-unlocks when they complete
            priority: "critical" / "high" / "medium" (default) / "low"
            horizon: "short" (default) / "mid" / "long"
            tags: Free-form tags for filtering the wall
            assigned_to: Agent name/id this task is meant for (optional)

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
        if priority:
            payload["priority"] = priority
        if horizon:
            payload["horizon"] = horizon
        if tags is not None:
            payload["tags"] = tags
        if assigned_to:
            payload["assigned_to"] = assigned_to
        return _api_call("POST", f"/api/teams/{team_id}/tasks/run", payload)

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
            task_type: Deprecated (pipeline retired, see design doc §7) — accepted
                for backward compatibility but no longer attaches a pipeline.
                Use CC Workflow (ultracode) for orchestration; runs are tracked
                on the /workflows observability page.

        Returns:
            Created task info
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
        # task_type 软退役（pipeline 已定向废弃，设计文档 §7 Phase1 断新增入口）：
        # 参数保留以兼容既有调用方，但不再自动挂载 pipeline；编排请改用 CC Workflow
        #（ultracode），运行档案见 workflow_list / Dashboard /workflows。
        if task_type and result.get("success"):
            result["message"] = (
                result.get("message", "任务已创建")
                + "（提示：task_type 已废弃，未挂载 pipeline；编排请用 CC Workflow）"
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

    @mcp.tool(meta={"anthropic/maxResultSizeChars": 500000})
    def task_list_project(
        project_id: str = "",
        team_id: str = "",
        horizon: str = "",
        priority: str = "",
        limit: int = 50,
        offset: int = 0,
        include_completed: bool = False,
        status: str = "",
        fields: str = "compact",
    ) -> dict[str, Any]:
        """Get the task wall — project-scoped by default, team-scoped on request.

        This is the single task-wall entry point (the team-only `taskwall_view`
        was folded in here 2026-07-27): pass `team_id` to narrow the wall to one
        team, leave it empty to get every team under the project plus the
        project-level tasks that belong to no team.

        Default response is a COMPACT projection (marked by view="compact" +
        hint — it is a trimmed view, NOT missing fields): each task row keeps
        id/title/priority/status/score/assigned_to/tags + 80-char desc excerpt
        (plus result/depends_on/subtask_count when present). Full details of a
        single task: task_status(task_id) / task_memo_read(task_id).

        Args:
            project_id: Project ID (optional, auto-uses active project if empty;
                ignored when team_id is given)
            team_id: Team ID or name — narrows the wall to one team (optional)
            horizon: Filter by time horizon: "short" / "mid" / "long" (optional)
            priority: Filter by priority: "critical" / "high" / "medium" / "low"
                (optional; comma-separated accepted for multiple)
            limit: Max number of active tasks to return (default 50; project scope only)
            offset: Pagination offset for active tasks (default 0; project scope only)
            include_completed: Include completed tasks (default False; project scope only)
            status: Filter by status: pending/running/blocked/completed
                (default all active; project scope only)
            fields: "compact" (default, trimmed projection) / "all" (full rows)

        Returns:
            Task wall with wall (grouped by horizon), completed tasks (project
            scope), and stats; compact view adds view + hint self-identification
        """
        view = resolve_view(fields)
        if view is None:
            return {"success": False, "error": FIELDS_ERROR}
        params: list[str] = []
        if horizon:
            params.append(f"horizon={urllib.parse.quote(horizon)}")
        if priority:
            params.append(f"priority={urllib.parse.quote(priority)}")
        if team_id:
            # Team scope: the team wall endpoint takes only horizon/priority.
            qs = f"?{'&'.join(params)}" if params else ""
            path = f"/api/teams/{urllib.parse.quote(team_id)}/task-wall{qs}"
        else:
            resolved = _resolve_project_id(project_id)
            if not resolved:
                return {
                    "success": False,
                    "error": "未找到活跃项目，请提供 project_id / team_id，或先创建项目",
                }
            params = [
                f"limit={limit}",
                f"offset={offset}",
                f"include_completed={'true' if include_completed else 'false'}",
                *params,
            ]
            if status:
                params.append(f"status={urllib.parse.quote(status)}")
            path = f"/api/projects/{resolved}/task-wall?{'&'.join(params)}"
        result = _api_call("GET", path)
        # 精简投影只作用于成功的墙结构；错误响应/全量视图原样透传
        if view == "all" or not isinstance(result, dict) or "wall" not in result:
            return result
        out: dict[str, Any] = {
            "wall": {
                h: [compact_task_row(t) for t in rows or []]
                for h, rows in (result.get("wall") or {}).items()
            },
            "stats": result.get("stats"),
            "view": "compact",
            "hint": TASK_WALL_HINT,
        }
        if "completed" in result:
            out["completed"] = [
                compact_task_row(t) for t in result.get("completed") or []
            ]
        return out

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
        supersedes: str | None = None,
    ) -> dict[str, Any]:
        """Add a memo record to a task — for tracking progress, recording decisions, marking issues.

        Args:
            task_id: Task ID
            content: Memo content
            memo_type: Type, one of "progress" / "decision" / "issue" / "summary"
            author: Author name, default "leader"
            supersedes: Optional memo ID this entry replaces; the old memo is
                marked invalid (Zep 失效语义，不删除)

        Returns:
            Added memo record
        """
        body: dict[str, Any] = {
            "content": content,
            "type": memo_type,
            "author": author,
        }
        if supersedes:
            body["supersedes"] = supersedes
        return _api_call(
            "POST",
            f"/api/tasks/{task_id}/memo",
            body,
        )

    @mcp.tool()
    def task_execution_trace(task_id: str, include_stats: bool = False) -> dict[str, Any]:
        """Get a task's execution timeline — plain, or with checkpoints + stats.

        The separate `task_replay` tool was folded in here 2026-07-27: both
        answered "how did this task actually go", differing only in whether the
        answer carried the derived summary. `include_stats=True` is the old
        replay view.

        Args:
            task_id: Task ID
            include_stats: False (default) — timeline only (memo records + task
                lifecycle events, chronological). True — adds `checkpoints`
                (decision/summary points only) and `stats` (duration, step count,
                subtask count, memo-type breakdown).

        Returns:
            task + timeline + total_events; with include_stats also checkpoints
            and stats
        """
        path = "replay" if include_stats else "execution-trace"
        return _api_call("GET", f"/api/tasks/{task_id}/{path}")
