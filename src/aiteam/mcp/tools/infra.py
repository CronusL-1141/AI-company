"""Infrastructure and OS-level MCP tools."""

from __future__ import annotations

import os
import urllib.parse
from typing import Any

from aiteam.mcp._base import API_URL, _api_call


def register(mcp):
    """Register all infrastructure MCP tools."""

    @mcp.tool()
    def context_resolve() -> dict[str, Any]:
        """Get the current active OS context — active project, active team, member list, loop status.

        This is the infrastructure for all simplified operations. A single call returns
        the complete context of the current working environment, allowing Leader or other
        tools to auto-fill parameters like project_id, team_id, etc.

        Returns:
            Context dict containing project / team / agents / loop
        """
        result: dict[str, Any] = {"project": None, "team": None, "agents": [], "loop": None}

        try:
            projects_data = _api_call("GET", "/api/projects")
            projects = projects_data.get("data", [])
            if projects:
                cwd = os.getcwd().replace("\\", "/").rstrip("/").lower()
                for p in projects:
                    rp = (p.get("root_path") or "").replace("\\", "/").rstrip("/").lower()
                    if rp and (cwd == rp or cwd.startswith(rp + "/")):
                        result["project"] = {"id": p["id"], "name": p.get("name", "")}
                        break

            teams_data = _api_call("GET", "/api/teams")
            active_teams = [t for t in teams_data.get("data", []) if t.get("status") == "active"]
            if active_teams:
                team = active_teams[0]
                result["team"] = {"id": team["id"], "name": team["name"]}
                agents_data = _api_call("GET", f"/api/teams/{team['id']}/agents")
                result["agents"] = [
                    {"name": a["name"], "status": a["status"], "role": a.get("role", "")}
                    for a in agents_data.get("data", [])
                ]

            if result["team"]:
                loop_data = _api_call("GET", f"/api/teams/{result['team']['id']}/loop/status")
                if loop_data.get("success") is not False:
                    result["loop"] = loop_data.get("data") or loop_data

        except Exception as e:
            result["error"] = str(e)

        return result

    @mcp.tool()
    def os_health_check() -> dict[str, Any]:
        """Check the health status of the AI Team OS API service.

        Verifies the API service is running normally by accessing the team list endpoint.

        Returns:
            Health status info including API reachability and team count
        """
        result = _api_call("GET", "/api/teams")
        if result.get("success") is False:
            return {
                "status": "unhealthy",
                "api_url": API_URL,
                "error": result.get("error", "未知错误"),
                "hint": result.get("hint", "请确保 FastAPI 服务已启动: aiteam serve"),
            }
        return {
            "status": "healthy",
            "api_url": API_URL,
            "teams_count": result.get("total", 0),
        }

    @mcp.tool()
    def os_report_issue(
        team_id: str,
        title: str,
        description: str = "",
        severity: str = "medium",
        category: str = "bug",
    ) -> dict[str, Any]:
        """Report an issue to the team. Issues are created as high-priority tasks, auto-tagged as issue type.

        Severity maps to task priority: critical->critical, high->high, medium->high, low->medium.

        Args:
            team_id: Team ID or name
            title: Issue title
            description: Detailed issue description
            severity: Severity level, one of "critical" / "high" / "medium" / "low"
            category: Issue category, e.g., "bug" / "performance" / "security" / "ux"

        Returns:
            Created Issue task info
        """
        return _api_call(
            "POST",
            f"/api/teams/{team_id}/issues",
            {
                "title": title,
                "description": description,
                "severity": severity,
                "category": category,
            },
        )

    @mcp.tool()
    def os_resolve_issue(issue_id: str, resolution: str) -> dict[str, Any]:
        """Mark an Issue as resolved with a resolution description.

        Updates the Issue status to resolved and records the resolution.
        The corresponding task is also marked as completed.

        Args:
            issue_id: Issue (task) ID
            resolution: Resolution description

        Returns:
            Updated Issue info
        """
        return _api_call(
            "PUT",
            f"/api/issues/{issue_id}/status",
            {
                "status": "resolved",
                "resolution": resolution,
            },
        )

    @mcp.tool()
    def event_list(limit: int = 50) -> dict[str, Any]:
        """List recent events in the system.

        Args:
            limit: Maximum number of events to return, default 50

        Returns:
            Event list with event type, source, and timestamp
        """
        return _api_call("GET", f"/api/events?limit={limit}")

    @mcp.tool()
    def find_skill(
        task_description: str = "",
        level: int = 1,
        category: str = "",
        skill_id: str = "",
    ) -> dict[str, Any]:
        """Find ecosystem skills/plugins using a 3-layer progressive loading system.

        Layer 1 (quick recommend): Describe your task and get top 3-5 matching skills
            with one-line descriptions and install commands.
        Layer 2 (category browse): Browse all skills grouped by category
            (memory / code-quality / frontend / security / dev-workflow / etc.).
        Layer 3 (full detail): Get complete documentation for a single skill
            including features, OS complement relationship, and variants.

        Args:
            task_description: What you want to accomplish (used for level=1 matching).
                              Examples: "frontend ui design", "security audit web app",
                              "data science jupyter", "code review PR".
            level: Discovery depth — 1=quick (default), 2=category, 3=full detail.
            category: Category filter for level=2 (e.g., "frontend", "security").
                      Empty string returns all categories.
            skill_id: Skill identifier for level=3 detail lookup
                      (e.g., "vibesec", "superpowers", "claude-mem").

        Returns:
            Dict with level info, results, and hints for deeper exploration.
        """
        from aiteam.mcp.skill_registry import (
            find_skill_category,
            find_skill_detail,
            find_skill_quick,
        )

        if level == 3:
            if not skill_id:
                return {
                    "error": "level=3 requires skill_id parameter.",
                    "hint": "Use level=1 with task_description to discover skill IDs first.",
                }
            return find_skill_detail(skill_id)

        if level == 2:
            return find_skill_category(category)

        if not task_description:
            return {
                "error": "level=1 requires task_description parameter.",
                "hint": "Describe what you want to do, e.g. 'build a secure REST API'.",
            }
        return find_skill_quick(task_description)

    @mcp.tool()
    def send_notification(message: str, urgency: str = "medium") -> dict[str, Any]:
        """Send a notification to the configured Slack webhook.

        Requires SLACK_WEBHOOK_URL to be configured via PUT /api/settings/webhook.

        Args:
            message: Notification message text.
            urgency: Urgency level — "low", "medium" (default), or "high".

        Returns:
            Result dict with ok/error fields.
        """
        return _api_call(
            "POST",
            "/api/settings/webhook/send",
            {"message": message, "urgency": urgency},
        )

    @mcp.tool()
    def ecosystem_recipes(recipe_id: str = "") -> dict[str, Any]:
        """List available ecosystem integration recipes for combining AI Team OS with external tools.

        Recipes describe how to integrate external MCP servers (GitHub, Slack, Linear, etc.)
        with AI Team OS workflows. Each recipe includes: recommended MCP server, install config,
        and concrete collaboration scenarios with AI Team OS tools.

        Args:
            recipe_id: Optional recipe identifier to get details for a specific recipe.
                       Leave empty to list all available recipes.
                       Valid IDs: "github", "slack", "linear", "fullstack-team".

        Returns:
            Dict with recipe list (overview) or single recipe detail.
        """
        recipes = {
            "github": {
                "id": "github",
                "name": "GitHub 集成",
                "mcp_server": "@modelcontextprotocol/server-github",
                "oneliner": "Code management, PR review, and Issue tracking with AI Team OS orchestration",
                "install_hint": 'npx -y @modelcontextprotocol/server-github (set GITHUB_PERSONAL_ACCESS_TOKEN)',
                "scenarios": [
                    "Pipeline deploy stage -> git_auto_commit + git_create_pr",
                    "Code review -> debate_code_review + GitHub PR comments",
                    "Issue tracking -> GitHub Issue <-> AI Team OS task wall sync",
                ],
                "os_tools_used": [
                    "git_auto_commit", "git_create_pr", "git_status_check",
                    "debate_code_review", "task_create", "task_update",
                ],
            },
            "slack": {
                "id": "slack",
                "name": "Slack 集成",
                "mcp_server": "@modelcontextprotocol/server-slack",
                "oneliner": "Team notifications, alerts, and standup summaries via Slack channels",
                "install_hint": 'npx -y @modelcontextprotocol/server-slack (set SLACK_BOT_TOKEN, SLACK_TEAM_ID)',
                "scenarios": [
                    "Leader briefing -> team_briefing + Slack channel push",
                    "Error budget RED -> budget_status alert to #ops-alerts",
                    "Daily standup -> taskwall_view summary to #standup",
                ],
                "os_tools_used": [
                    "team_briefing", "send_notification", "briefing_list",
                    "budget_status", "taskwall_view", "meeting_conclude",
                ],
            },
            "linear": {
                "id": "linear",
                "name": "Linear 集成",
                "mcp_server": "@modelcontextprotocol/server-linear",
                "oneliner": "Sync Linear Issues with AI Team OS tasks and map Sprints to Pipelines",
                "install_hint": 'npx -y @modelcontextprotocol/server-linear (set LINEAR_API_KEY)',
                "scenarios": [
                    "Linear Issue <-> AI Team OS task sync",
                    "Sprint stages -> Pipeline stages mapping (Backlog->plan, In Progress->develop, etc.)",
                    "Bi-directional status sync on task completion",
                ],
                "os_tools_used": [
                    "task_create", "task_update", "pipeline_create",
                    "pipeline_advance", "pipeline_status", "task_memo_add",
                ],
            },
            "fullstack-team": {
                "id": "fullstack-team",
                "name": "全栈开发团队模板",
                "mcp_server": "AI Team OS + GitHub + Slack (combo)",
                "oneliner": "Pre-configured fullstack team with frontend, backend, QA, and DevOps roles",
                "install_hint": "See docs/ecosystem-recipes.md for full .mcp.json config",
                "scenarios": [
                    "Sprint start -> sync GitHub Issues to task wall",
                    "Dev phase -> frontend + backend + VibeSec security scan",
                    "Review phase -> debate_code_review + GitHub PR + E2E tests",
                    "Deploy phase -> Docker build + git_auto_commit + pipeline_advance",
                    "Retrospective -> loop_review + team_briefing -> Slack push",
                ],
                "os_tools_used": [
                    "project_create", "team_create", "agent_register",
                    "loop_start", "taskwall_view", "debate_code_review",
                    "git_auto_commit", "git_create_pr", "pipeline_advance",
                    "loop_review", "team_briefing", "meeting_conclude",
                ],
                "recommended_skills": ["Superpowers", "VibeSec", "Frontend-Design"],
            },
        }

        if recipe_id:
            rid = recipe_id.lower().strip()
            recipe = recipes.get(rid)
            if recipe is None:
                return {
                    "error": f"Recipe '{recipe_id}' not found.",
                    "available_recipes": [
                        {"id": r["id"], "name": r["name"], "oneliner": r["oneliner"]}
                        for r in recipes.values()
                    ],
                }
            return {
                "recipe": recipe,
                "docs": "See docs/ecosystem-recipes.md for full setup guide with .mcp.json examples.",
            }

        return {
            "recipes": [
                {"id": r["id"], "name": r["name"], "oneliner": r["oneliner"]}
                for r in recipes.values()
            ],
            "hint": "Use ecosystem_recipes(recipe_id='github') for detailed setup info.",
            "docs": "See docs/ecosystem-recipes.md for complete integration guides.",
        }

    @mcp.tool()
    def cross_project_send(
        content: str,
        to_project_id: str = "",
        message_type: str = "notification",
        sender_name: str = "system",
    ) -> dict[str, Any]:
        """Send a message to another project (or broadcast to all).

        Messages are stored in the shared global DB so any project can read them.
        Requires PROJECT_DIR env var (set automatically by Claude Code via CLAUDE_PROJECT_DIR).

        Args:
            content: Message body text.
            to_project_id: Recipient's 12-char project ID. Leave empty to broadcast to all projects.
            message_type: One of "notification" / "request" / "response" / "broadcast".
            sender_name: Sender name shown in the recipient's inbox (default "system").

        Returns:
            Created cross-project message info including id, from_project_id, created_at.
        """
        payload: dict[str, Any] = {
            "content": content,
            "sender_name": sender_name,
            "message_type": message_type,
            "metadata": {},
        }
        if to_project_id:
            payload["to_project_id"] = to_project_id
        return _api_call("POST", "/api/cross-messages", payload)

    @mcp.tool()
    def cross_project_inbox(
        unread_only: bool = True,
        limit: int = 20,
    ) -> dict[str, Any]:
        """Read the cross-project message inbox for the current project.

        Returns direct messages sent to this project plus any broadcasts.
        Requires PROJECT_DIR env var (set automatically by Claude Code via CLAUDE_PROJECT_DIR).

        Args:
            unread_only: If True (default), only return unread messages.
            limit: Maximum number of messages to return (default 20).

        Returns:
            Inbox message list sorted newest-first, plus unread_count.
        """
        params = urllib.parse.urlencode({"unread_only": str(unread_only).lower(), "limit": limit})
        inbox = _api_call("GET", f"/api/cross-messages?{params}")
        count = _api_call("GET", "/api/cross-messages/count")
        if isinstance(inbox, dict) and isinstance(count, dict):
            inbox["unread_count"] = count.get("data", 0)
        return inbox
