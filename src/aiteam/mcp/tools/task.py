"""Task management MCP tools."""

from __future__ import annotations

import json
import urllib.parse
from typing import Any

from aiteam.mcp._base import _api_call, _resolve_project_id
from aiteam.mcp.tools.views import (
    FIELDS_ERROR,
    TASK_WALL_HINT,
    compact_task_row,
    compact_task_write_response,
    resolve_view,
)
from aiteam.types import MemoType


def _write_call(method, path, payload, fields):
    view = resolve_view(fields)
    if view is None:
        return {"success": False, "error": FIELDS_ERROR}
    result = _api_call(method, path, payload)
    # Native _api_call keeps HTTP error bodies as JSON strings. Normalize only
    # task write errors here; other tool families retain their existing contract.
    if isinstance(result, dict) and result.get("error"):
        detail = result.get("detail")
        if isinstance(detail, str):
            try:
                parsed = json.loads(detail)
                detail = parsed.get("detail", parsed) if isinstance(parsed, dict) else parsed
            except (ValueError, TypeError):
                pass
        result = {**result, "success": False, "detail": detail}
        if isinstance(detail, dict):
            for key in ("current_revision", "current_task", "open_subtasks", "blocked_by", "waiting_on"):
                if key in detail:
                    result[key] = detail[key]
            if view == "compact" and isinstance(result.get("current_task"), dict):
                projected = compact_task_write_response({"success": True, "data": result["current_task"]})["data"]
                result["current_task"] = projected
                result["detail"] = {**detail, "current_task": projected}
    return compact_task_write_response(result) if view == "compact" else result


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
        parent_id: str = "",
        auto_start: bool = False,
        waiting_on: dict[str, Any] | None = None,
        status_note: str = "",
        fields: str = "all",
    ) -> dict[str, Any]:
        """Put a task on a team's wall. Despite the name, nothing executes it.

        This tool only creates the task row. Dispatch it yourself (Agent(...) /
        SendMessage); the sub-agent then writes progress back with
        task_memo_add.

        Priority and horizon drive the task wall's ordering, so set them here.

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
            parent_id: Open top-level parent task in the team's project (optional)
            auto_start: Start status only; dependencies/waiting keep it blocked, no dispatch
            waiting_on: User/agent wait with kind, target_id and reason (optional)
            status_note: Human-readable current status explanation
            fields: "all" (default, compatible) / "compact" write receipt

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
        if parent_id:
            payload["parent_id"] = parent_id
        payload.update(auto_start=auto_start, status_note=status_note)
        if waiting_on is not None:
            payload["waiting_on"] = waiting_on
        return _write_call("POST", f"/api/teams/{team_id}/tasks/run", payload, fields)

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
        parent_id: str = "",
        depends_on: list[str] | None = None,
        assigned_to: str = "",
        waiting_on: dict[str, Any] | None = None,
        status_note: str = "",
        fields: str = "all",
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
            auto_start: Request running status; incomplete dependencies/waiting return blocked, without dispatch
            task_type: Ignored; accepted only so existing callers keep working.
                For orchestration use a CC Workflow (ultracode); its runs are
                tracked by workflow_list / workflow_get.
            parent_id: Open top-level parent task in the same project (optional)
            depends_on: Same-project dependency IDs (optional)
            assigned_to: Agent ID/name; assignment does not dispatch it
            waiting_on: Wait object with kind=user/agent, target_id and reason
            status_note: Current status explanation
            fields: "all" (default, compatible) / "compact" write receipt

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
        if parent_id:
            payload["parent_id"] = parent_id
        payload.update(auto_start=auto_start, status_note=status_note)
        if depends_on is not None:
            payload["depends_on"] = depends_on
        if assigned_to:
            payload["assigned_to"] = assigned_to
        if waiting_on is not None:
            payload["waiting_on"] = waiting_on
        result = _write_call("POST", f"/api/projects/{resolved}/tasks", payload, fields)
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
        """Get one task's full record (every task field, not a trimmed row).

        Includes status, result, description, tags, dependencies, and
        timestamps. task_list_project returns the wall as trimmed rows;
        task_memo_read returns the task's memo history.

        Args:
            task_id: Task ID

        Returns:
            Task details including status, result, etc.
        """
        return _api_call("GET", f"/api/tasks/{task_id}")

    @mcp.tool()
    def task_update(
        task_id: str,
        status: str | None = None,
        assigned_to: str | None = None,
        result: str | None = None,
        priority: str | None = None,
        tags: list[str] | None = None,
        title: str | None = None,
        description: str | None = None,
        horizon: str | None = None,
        parent_id: str | None = None,
        depends_on: list[str] | None = None,
        waiting_on: dict[str, Any] | None = None,
        status_note: str | None = None,
        expected_revision: int | None = None,
        fields: str = "all",
    ) -> dict[str, Any]:
        """Update task fields; omitted values stay unchanged, empty text clears.

        Completion shares the HTTP complete path's validation and dependency unlock.

        Args:
            task_id: Task ID
            status: pending/blocked/running/completed/failed; blocked explicitly means manual
            assigned_to: Agent name/ID; empty string clears assignment
            result: Result text; empty string clears it
            priority: critical/high/medium/low
            tags: Tag list; [] clears it
            title: New non-empty title; omitted keeps it
            description: Description; empty string clears it
            horizon: short/mid/long
            parent_id: Same-project parent ID; empty string detaches, omitted keeps it
            depends_on: Same-project dependency IDs; [] clears, omitted keeps
            waiting_on: kind=user/agent, target_id and reason; {} clears waiting
            status_note: Current status explanation; empty string clears
            expected_revision: Revision observed before editing; mismatches return conflict
            fields: "all" (default, compatible) / "compact" write receipt
        """
        payload = {}
        for name, value in {
            "status": status, "assigned_to": assigned_to, "result": result, "priority": priority,
            "tags": tags, "title": title, "description": description, "horizon": horizon,
            "depends_on": depends_on, "status_note": status_note, "expected_revision": expected_revision,
        }.items():
            if value is not None:
                payload[name] = value
        if parent_id is not None:
            payload["parent_id"] = parent_id or None
        if waiting_on is not None:
            payload["waiting_on"] = waiting_on or None
        return _write_call("PUT", f"/api/tasks/{task_id}", payload, fields)

    @mcp.tool()
    def task_create_batch(
        tasks: list[dict[str, Any]],
        idempotency_key: str,
        project_id: str = "",
        fields: str = "all",
    ) -> dict[str, Any]:
        """Create an atomic task batch; retrying the same key and payload replays it.

        Args:
            tasks: 1-100 tasks with unique key/title; parent_id/depends_on accept @key references
            idempotency_key: Stable caller batch key; conflicting parameters are refused
            project_id: Project ID (optional, active project when empty)
            fields: "all" (default, compatible) / "compact" write receipt
        """
        resolved = _resolve_project_id(project_id)
        if not resolved:
            return {"success": False, "error": "未找到活跃项目，请提供 project_id"}
        return _write_call("POST", f"/api/projects/{resolved}/tasks/batch",
                           {"tasks": tasks, "idempotency_key": idempotency_key}, fields)

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

        Pass `team_id` to narrow the wall to one team; leave it empty to get
        every team under the project plus the project-level tasks that belong
        to no team.

        Project scope leads with `digest`: the whole wall in one text block
        (open counts by status and horizon, 7-day trend, stale and dormant
        counts, the 5 most recent actions, the top 5 pending, the first mid
        and long pending task). It is the same block the session briefing
        shows; read it first, the rows below are one page.

        Default response is a COMPACT projection (marked by view="compact" +
        hint — it is a trimmed view, NOT missing fields): each task row keeps
        id/title/priority/status/score/assigned_to/tags + 80-char desc excerpt
        (plus result/depends_on/subtask_count when present). Full details of a
        single task: task_status(task_id) / task_memo_read(task_id).

        Args:
            project_id: Project ID (optional, auto-uses active project if empty;
                ignored when team_id is given)
            team_id: Team UUID - narrows the wall to one team (optional). Names
                are not resolved here and return an empty wall; get the id
                from team_list
            horizon: Filter by time horizon: "short" / "mid" / "long" (optional)
            priority: Filter by priority: "critical" / "high" / "medium" / "low"
                (optional; comma-separated accepted for multiple)
            limit: Max number of pending tasks to return (default 50; project scope
                only). Running, blocked and failed tasks always come back in full;
                not_shown counts the pending tasks left off the page.
            offset: Pagination offset for pending tasks (default 0; project scope only)
            include_completed: Include completed tasks (default False; project scope only)
            status: Filter by status: pending/running/blocked/failed/completed
                (default: every status except completed; project scope only).
                status="completed" returns rows only together with
                include_completed=True; on its own it yields an empty wall.
            fields: "compact" (default, trimmed projection) / "all" (full rows)

        Returns:
            Task wall with digest (project scope; text in the compact view, the
            full JSON with fields="all"), wall (grouped by horizon), completed
            tasks (project scope), and stats; compact view adds view + hint
            self-identification
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
        out: dict[str, Any] = {}
        digest = result.get("digest")
        if isinstance(digest, dict) and digest.get("text"):
            out["digest"] = digest["text"]
        out |= {
            "wall": {
                h: [compact_task_row(t) for t in rows or []]
                for h, rows in (result.get("wall") or {}).items()
            },
            "stats": result.get("stats"),
            "view": "compact",
            "hint": TASK_WALL_HINT,
        }
        # Project scope pages the pending tasks only and says how many it left out.
        for key in ("not_shown", "has_more"):
            if key in result:
                out[key] = result[key]
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
        memo_type: MemoType = "progress",
        author: str = "leader",
        supersedes: str | None = None,
    ) -> dict[str, Any]:
        """Add a memo record to a task — for tracking progress, recording decisions, marking issues.

        Args:
            task_id: Task ID
            content: Memo content
            memo_type: Type, one of "progress" / "decision" / "issue" / "summary"
            author: Your agent name (sub-agents: the OS name given at start).
                The server links this task to the agent of that name in the
                current session for usage attribution; keep the default "leader"
                only in the leader session.
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

        Answers "how did this task actually go"; include_stats=True adds the
        derived summary on top of the timeline.

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
