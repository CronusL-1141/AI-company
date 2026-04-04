"""Memory and knowledge MCP tools."""

from __future__ import annotations

import urllib.parse
from typing import Any

from aiteam.mcp._base import _api_call, _resolve_team_id


def register(mcp):
    """Register all memory-related MCP tools."""

    @mcp.tool()
    def memory_search(
        query: str = "",
        scope: str = "global",
        scope_id: str = "system",
        limit: int = 10,
    ) -> dict[str, Any]:
        """Search the memory store in AI Team OS.

        Args:
            query: Search keywords
            scope: Memory scope, default "global"
            scope_id: Scope ID, default "system"
            limit: Maximum number of results, default 10

        Returns:
            List of matching memories
        """
        params = urllib.parse.urlencode({"scope": scope, "scope_id": scope_id, "query": query, "limit": limit})
        return _api_call("GET", f"/api/memory?{params}")

    @mcp.tool()
    def team_knowledge(
        team_id: str = "",
        type: str = "",
        limit: int = 20,
    ) -> dict[str, Any]:
        """Query the team knowledge base — retrieve accumulated experience and lessons learned.

        Returns memories with scope=team for this team, including:
        - failure_alchemy: Lessons from failure alchemy
        - lesson_learned: Manually recorded experiences
        - loop_review: Loop review summaries

        New Agents should call this tool before joining to get team historical knowledge for quick onboarding.

        Args:
            team_id: Team ID (leave empty to auto-get active team)
            type: Type filter, one of failure_alchemy / lesson_learned / loop_review (empty returns all)
            limit: Maximum number of results, default 20

        Returns:
            Team knowledge memory list
        """
        resolved_id = _resolve_team_id(team_id)
        if not resolved_id:
            return {"success": False, "error": "未找到活跃团队，请传入 team_id"}
        params_dict: dict[str, Any] = {"limit": limit}
        if type:
            params_dict["type"] = type
        params = urllib.parse.urlencode(params_dict)
        return _api_call("GET", f"/api/teams/{resolved_id}/knowledge?{params}")
