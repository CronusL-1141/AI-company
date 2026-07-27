"""Team management MCP tools."""

from __future__ import annotations

from typing import Any

from aiteam.mcp._base import _api_call


def register(mcp):
    """Register all team-related MCP tools."""

    @mcp.tool()
    def team_status(team_id: str) -> dict[str, Any]:
        """Get a team's status summary — team info + members + active tasks.

        Hits /status (not the bare team row): the plain team endpoint carries no
        member or task fields, so callers asking "what is this team doing" got a
        row with nothing actionable in it.

        Args:
            team_id: Team ID or team name

        Returns:
            team (row), agents (members with status/current_task),
            active_tasks, completed_tasks, total_tasks
        """
        return _api_call("GET", f"/api/teams/{team_id}/status")

    @mcp.tool()
    def team_list() -> dict[str, Any]:
        """List all created teams.

        Returns:
            Team list with basic info for each team
        """
        return _api_call("GET", "/api/teams")

    @mcp.tool()
    def team_briefing(team_id: str) -> dict[str, Any]:
        """Get a team panoramic briefing — understand full team status in one call.

        Returns team info, member status, recent events, recent meetings, pending tasks, and action suggestions.

        Args:
            team_id: Team ID or team name

        Returns:
            Team panoramic briefing containing agents / recent_events / recent_meeting / pending_tasks / _hints
        """
        return _api_call("GET", f"/api/teams/{team_id}/briefing")

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
