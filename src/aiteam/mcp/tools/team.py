"""Team management MCP tools."""

from __future__ import annotations

from typing import Any

from aiteam.mcp._base import _api_call
from aiteam.mcp.tools.views import (
    FIELDS_ERROR,
    OFFLINE_PREVIEW_DEFAULT,
    TEAM_BRIEFING_HINT,
    TEAM_LIST_HINT,
    TEAM_STATUS_HINT,
    compact_event_row,
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

        Hits /status (not the bare team row): the plain team endpoint carries no
        member or task fields, so callers asking "what is this team doing" got a
        row with nothing actionable in it.

        Default response is a COMPACT projection (view="compact" + hint - trimmed,
        NOT missing fields). The upstream summary embeds the entire roster and
        every active task as full rows, which measured 170,331 chars on a real
        173-member workflow team and 69,660 on a 51-member session team - both
        past the MCP result ceiling, i.e. the tool simply did not work on the
        teams that most needed it. Members and tasks are projected here, offline
        members fold into a count plus digest, and the API route and Dashboard
        JSON are untouched.

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
        project_id / created_at. The unfiltered full-row list measured 148,173
        chars across 316 teams on the real install, past the MCP result ceiling;
        teams accumulate one row per Workflow run and per CC session, so the list
        only ever grows.

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

    @mcp.tool()
    def team_briefing(
        team_id: str,
        fields: str = "compact",
        include_offline: bool = False,
        limit: int = 30,
        offline_preview: int = OFFLINE_PREVIEW_DEFAULT,
    ) -> dict[str, Any]:
        """Get a team panoramic briefing — understand full team status in one call.

        Returns team info, member status, recent events, recent meetings, pending tasks, and action suggestions.

        Default response is a COMPACT projection (view="compact" + hint - trimmed,
        NOT missing fields): the roster follows the same live-first / offline-digest
        split as agent_list, event payloads collapse to a one-line derived summary,
        and pending tasks use the task-wall row projection. Measured 20,521 chars
        on a 173-member workflow team before projection.

        Args:
            team_id: Team ID or team name
            fields: "compact" (default, trimmed rows) / "all" (full briefing)
            include_offline: Include offline members as rows instead of a count
                plus digest (default False)
            limit: Max member rows to return after the offline split (default 30,
                capped at 200)
            offline_preview: How many most-recent offline members to show in the
                digest (default 5; ignored when include_offline is True)

        Returns:
            Team panoramic briefing containing agents / recent_events / recent_meeting / pending_tasks / _hints
        """
        view = resolve_view(fields)
        if view is None:
            return {"success": False, "error": FIELDS_ERROR}
        envelope = _api_call("GET", f"/api/teams/{team_id}/briefing")
        result = envelope.get("data") if isinstance(envelope, dict) else None
        if not isinstance(result, dict) or "agents" not in result:
            return envelope
        roster = project_roster(
            result.get("agents") or [],
            include_offline=include_offline,
            offline_preview=offline_preview,
            view=view,
        )
        members, _size, has_more = page(roster["members"], limit, 0)
        events = [e for e in (result.get("recent_events") or []) if isinstance(e, dict)]
        pending = [t for t in (result.get("pending_tasks") or []) if isinstance(t, dict)]
        out: dict[str, Any] = {
            "team": result.get("team"),
            "agents": members,
            "member_total": roster["counted"],
            "status_counts": roster["status_counts"],
            "members_has_more": has_more,
            "recent_events": events if view == "all" else [compact_event_row(e) for e in events],
            "recent_meeting": result.get("recent_meeting"),
            "pending_tasks": pending if view == "all" else [compact_task_row(t) for t in pending],
            "file_hotspots": result.get("file_hotspots"),
            "_hints": result.get("_hints"),
            "view": view,
            "hint": TEAM_BRIEFING_HINT,
        }
        if "offline" in roster:
            out["offline"] = roster["offline"]
        return out

    @mcp.tool()
    def team_close(team_id: str = "") -> dict[str, Any]:
        """Close (complete) a team — sets team status to completed and marks all busy agents as offline.

        Use this when the team's mission is fully done. Members are not deleted,
        but their status is set to offline automatically.

        team_id is REQUIRED — closing a team is not reversible from the tool面, so
        it never falls back to auto-resolving "the active team".

        Args:
            team_id: Team ID or name (required — use team_list to find it)

        Returns:
            Updated team info with status=completed
        """
        # 不可逆动作禁用空参解析：自动解析出的"活跃队"可能是 workflow per-run 队
        # 或别会话的容器队，关错队会把别人正在用的团队连人带状态一起收工。
        if not team_id.strip():
            return {
                "success": False,
                "error": "team_close 必须显式提供 team_id（关队不可逆，不做自动解析）",
                "_recovery": "先用 team_list 找到目标团队 id，再显式传入。",
            }
        return _api_call("PUT", f"/api/teams/{team_id}", {"status": "completed"})

    @mcp.tool()
    def team_delete(team_id: str) -> dict[str, Any]:
        """Delete a team. team_id is REQUIRED — never auto-resolved.

        Args:
            team_id: Team ID or name to delete

        Returns:
            Deletion result
        """
        # 同 team_close：删队绝不靠猜。空串曾经会落进 _resolve_team_id 的
        # "随便挑一支活跃队"分支，等于把删除动作指向未知对象。
        if not team_id.strip():
            return {
                "success": False,
                "error": "team_delete 必须显式提供 team_id（删队不可逆，不做自动解析）",
                "_recovery": "先用 team_list 确认目标团队 id，再显式传入。",
            }
        return _api_call("DELETE", f"/api/teams/{team_id}")
