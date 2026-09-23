"""Team management MCP tools."""

from __future__ import annotations

from typing import Any

from aiteam.mcp._base import _api_call
from aiteam.mcp.tools.views import (
    FIELDS_ERROR,
    OFFLINE_PREVIEW_DEFAULT,
    TEAM_LIST_HINT,
    TEAM_STATUS_HINT,
    compact_task_row,
    compact_team_row,
    page,
    project_roster,
    resolve_view,
)

# team_status 里 active_tasks 的展示帽子：超出部分只报数并指路 task_list_project，
# 免得一支任务爆炸的团队把状态摘要重新撑爆（名册刚修好，别从另一条边漏回去）。
_ACTIVE_TASK_CAP = 30


def register(mcp):
    """Register all team-related MCP tools."""

    @mcp.tool()
    def team_status(
        team_id: str,
        fields: str = "compact",
        include_offline: bool = False,
        limit: int = 30,
        offline_preview: int = OFFLINE_PREVIEW_DEFAULT,
    ) -> dict[str, Any]:
        """Get a team's status summary — team info + members + active tasks.

        Default response is a COMPACT projection (view="compact" + hint - trimmed,
        NOT missing fields): member and task rows are projected, offline members
        fold into a count plus digest, and at most 30 active tasks are listed
        (the remainder is reported in active_tasks_omitted; task_list_project
        with team_id lists them all).

        Args:
            team_id: Team ID or team name
            fields: "compact" (default, trimmed rows) / "all" (full member and task rows)
            include_offline: Include offline members as rows instead of a count
                plus digest (default False)
            limit: Max member rows to return after the offline split (default 30,
                capped at 200)
            offline_preview: How many most-recent offline members to show in the
                digest (default 5; ignored when include_offline is True)

        Returns:
            team, members (projected), member_total / status_counts / offline
            digest, active_tasks, completed_tasks, total_tasks, plus view + hint
        """
        view = resolve_view(fields)
        if view is None:
            return {"success": False, "error": FIELDS_ERROR}
        envelope = _api_call("GET", f"/api/teams/{team_id}/status")
        # 错误响应/结构不认识就原样透传——投影只作用在成功的摘要上。
        result = envelope.get("data") if isinstance(envelope, dict) else None
        if not isinstance(result, dict) or "agents" not in result:
            return envelope
        roster = project_roster(
            result.get("agents") or [],
            include_offline=include_offline,
            offline_preview=offline_preview,
            view=view,
        )
        members, size, has_more = page(roster["members"], limit, 0)
        tasks = [t for t in (result.get("active_tasks") or []) if isinstance(t, dict)]
        shown = tasks if view == "all" else [compact_task_row(t) for t in tasks[:_ACTIVE_TASK_CAP]]
        out: dict[str, Any] = {
            "team": result.get("team") if view == "all" else compact_team_row(result.get("team") or {}),
            "members": members,
            "member_total": roster["counted"],
            "status_counts": roster["status_counts"],
            "member_limit": size,
            "members_has_more": has_more,
            "active_tasks": shown,
            "active_task_total": len(tasks),
            "completed_tasks": result.get("completed_tasks"),
            "total_tasks": result.get("total_tasks"),
            "view": view,
            "hint": TEAM_STATUS_HINT,
        }
        if "offline" in roster:
            out["offline"] = roster["offline"]
        if len(shown) < len(tasks):
            out["active_tasks_omitted"] = (
                f"另有 {len(tasks) - len(shown)} 个在办任务未展开，全量用 task_list_project(team_id=...)"
            )
        return out

    @mcp.tool()
    def team_list(
        fields: str = "compact",
        status: str = "active",
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        """List teams — active ones by default, newest first.

        Default response is a COMPACT projection (view="compact" + hint - trimmed,
        NOT missing fields): each row keeps id / name / status / kind /
        project_id / created_at. Teams accumulate one row per Workflow run and
        per CC session, so the list is long: filter by status and page with
        limit / offset.

        Args:
            fields: "compact" (default, trimmed rows) / "all" (full team rows)
            status: Filter by lifecycle status - "active" (default) / "completed"
                / "archived" / "" for every team
            limit: Max teams to return (default 50, capped at 200)
            offset: Pagination offset (default 0)

        Returns:
            teams (projected rows), total (before paging), matched, paging flags,
            plus view + hint self-identification
        """
        view = resolve_view(fields)
        if view is None:
            return {"success": False, "error": FIELDS_ERROR}
        result = _api_call("GET", "/api/teams")
        if not isinstance(result, dict) or "data" not in result:
            return result
        rows = [t for t in (result.get("data") or []) if isinstance(t, dict)]
        wanted = (status or "").strip().lower()
        matched = [t for t in rows if not wanted or str(t.get("status") or "") == wanted]
        matched.sort(key=lambda t: str(t.get("created_at") or ""), reverse=True)
        projected = matched if view == "all" else [compact_team_row(t) for t in matched]
        shown, size, has_more = page(projected, limit, offset)
        return {
            "teams": shown,
            "total": result.get("total", len(rows)),
            "matched": len(matched),
            "status_filter": wanted or "all",
            "limit": size,
            "offset": max(0, int(offset or 0)),
            "has_more": has_more,
            "view": view,
            "hint": TEAM_LIST_HINT,
        }
