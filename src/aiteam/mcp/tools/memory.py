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

    @mcp.tool()
    def pattern_record(
        type: str,
        task_type: str,
        template: str,
        approach: str,
        result: str = "",
        error: str = "",
        lesson: str = "",
    ) -> dict[str, Any]:
        """Record an agent execution pattern (success or failure) for future learning.

        Stores the pattern in the global execution pattern memory so future agents
        can benefit from this experience when tackling similar tasks.

        Args:
            type: "success" or "failure"
            task_type: Task category (e.g. "api-implementation", "bug-fix", "research")
            template: Agent template name that executed the task
            approach: Description of the approach taken
            result: Result summary (required for success patterns)
            error: Error description (required for failure patterns)
            lesson: Lesson learned (required for failure patterns)

        Returns:
            Record confirmation with memory_id
        """
        params = urllib.parse.urlencode({
            "pattern_type": type,
            "task_type": task_type,
            "agent_template": template,
            "approach": approach,
            "result": result,
            "error": error,
            "lesson": lesson,
        })
        return _api_call("POST", f"/api/execution-patterns/record?{params}")

    @mcp.tool()
    def pattern_search(
        query: str,
        top_k: int = 3,
    ) -> dict[str, Any]:
        """Search historical execution patterns similar to a task description.

        Uses BM25 retrieval to find relevant success/failure patterns recorded
        by agents in previous tasks. Use this before starting a complex task
        to benefit from past experience.

        Args:
            query: Task description or keywords to match against
            top_k: Maximum number of patterns to return (default 3, max 20)

        Returns:
            List of matching patterns with type, approach, and result/lesson
        """
        params = urllib.parse.urlencode({"query": query, "top_k": top_k})
        return _api_call("GET", f"/api/execution-patterns/search?{params}")
