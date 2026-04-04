"""Agent management MCP tools."""

from __future__ import annotations

import os
import urllib.parse
from typing import Any

from aiteam.mcp._base import _api_call, _resolve_team_id, logger


def _load_agent_prompt_template() -> str:
    """Load the standardized Agent prompt template."""
    # This file is at src/aiteam/mcp/tools/agent.py, need to go up 5 levels to project root
    template_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))),
        "plugin",
        "config",
        "agent-prompt-template.md",
    )
    try:
        with open(template_path, encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        logger.warning("Agent prompt模板文件不存在: %s", template_path)
        return ""


def _render_agent_prompt(role: str, project_path: str = "") -> str:
    """Fill the template with basic information."""
    template = _load_agent_prompt_template()
    if not template:
        return ""
    return template.replace("{role}", role).replace("{project_path}", project_path or "未指定")


def register(mcp):
    """Register all agent-related MCP tools."""

    @mcp.tool()
    def agent_register(
        team_id: str,
        name: str,
        role: str,
        model: str = "claude-opus-4-6",
        system_prompt: str = "",
    ) -> dict[str, Any]:
        """Register an Agent record in the database (internal/advanced use).

        NOTE: For normal workflow, use CC's Agent tool with team_name parameter instead.
        CC Agent tool spawns a real subprocess AND auto-registers via hooks.
        This MCP tool only creates a DB record — no actual agent process is started.

        Args:
            team_id: Target team ID or name
            name: Agent name
            role: Agent role description
            model: Model to use, default claude-opus-4-6
            system_prompt: Agent's system prompt

        Returns:
            Agent info
        """
        effective_prompt = system_prompt
        if not effective_prompt:
            effective_prompt = _render_agent_prompt(role)

        return _api_call(
            "POST",
            f"/api/teams/{team_id}/agents",
            {
                "name": name,
                "role": role,
                "model": model,
                "system_prompt": effective_prompt,
            },
        )

    @mcp.tool()
    def agent_update_status(
        agent_id: str,
        status: str,
    ) -> dict[str, Any]:
        """Update an Agent's running status.

        Args:
            agent_id: Agent ID
            status: New status, one of "busy", "waiting", "offline"

        Returns:
            Updated Agent info
        """
        return _api_call("PUT", f"/api/agents/{agent_id}/status", {"status": status})

    @mcp.tool()
    def agent_list(team_id: str) -> dict[str, Any]:
        """List all registered Agents in a team.

        Args:
            team_id: Team ID or name

        Returns:
            Agent list with status and role for each Agent
        """
        return _api_call("GET", f"/api/teams/{team_id}/agents")

    @mcp.tool()
    def agent_template_list() -> dict[str, Any]:
        """List all available Agent templates (from ~/.claude/agents/).

        Returns a template list and a grouped-by-category view to help choose the right Agent role template.

        Returns:
            templates: All template list
            grouped: Templates grouped by category
            total: Total template count
        """
        return _api_call("GET", "/api/agent-templates")

    @mcp.tool()
    def agent_template_recommend(task_type: str = "", keywords: str = "") -> dict[str, Any]:
        """Recommend suitable Agent templates based on task type and keywords.

        Args:
            task_type: Task type, e.g., "backend", "frontend", "data-analysis"
            keywords: Keywords, space-separated, e.g., "python api database"

        Returns:
            recommendations: Up to 5 matching templates sorted by relevance
            query: Actual query string used
        """
        params = urllib.parse.urlencode({"task_type": task_type, "keywords": keywords})
        return _api_call("GET", f"/api/agent-templates/recommend?{params}")

    @mcp.tool()
    def agent_activity_query(
        team_id: str = "",
        agent_id: str = "",
        limit: int = 20,
    ) -> dict[str, Any]:
        """Query Agent activity records for a team.

        Returns recent activity log entries sorted by timestamp descending,
        including action type, duration_ms, and result summary.

        Args:
            team_id: Team ID or name (optional, auto-uses active team if empty)
            agent_id: Filter by a specific Agent ID (optional, returns all agents if empty)
            limit: Maximum number of records to return, default 20

        Returns:
            Activity list with agent_name, action, timestamp, duration_ms, etc.
        """
        resolved = _resolve_team_id(team_id)
        if not resolved:
            return {"success": False, "error": "未找到活跃团队，请提供 team_id 或先创建团队"}
        params: list[str] = [f"limit={limit}"]
        if agent_id:
            params.append(f"agent_id={urllib.parse.quote(agent_id)}")
        qs = "?" + "&".join(params)
        return _api_call("GET", f"/api/teams/{resolved}/activities{qs}")
