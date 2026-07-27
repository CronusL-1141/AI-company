"""Leader briefing MCP tools."""

from __future__ import annotations

import urllib.parse
from typing import Any

from aiteam.mcp._base import _api_call, _resolve_project_id


def register(mcp):
    """Register all briefing-related MCP tools."""

    @mcp.tool()
    def briefing_add(
        title: str,
        description: str = "",
        options: str = "",
        recommendation: str = "",
        urgency: str = "medium",
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        """Add a decision item to Leader Briefing for user review.

        Use when Leader encounters decisions that require user input:
        project direction, architecture choices, budget/resource allocation.

        Anything a sub-agent's completion report leaves "for the user to decide"
        belongs here — a decision parked in report prose is a decision the user
        never actually received.

        Args:
            title: Brief description of the decision needed
            description: Detailed context
            options: Available choices (e.g. "A: option1 / B: option2")
            recommendation: Leader's suggested choice and reasoning
            urgency: high / medium / low
            tags: Free-form topic tags for filtering the queue (e.g. ["release"])
        """
        project_id = _resolve_project_id("")
        return _api_call(
            "POST",
            "/api/leader-briefings",
            {
                "title": title,
                "description": description,
                "options": options,
                "recommendation": recommendation,
                "urgency": urgency,
                "project_id": project_id,
                "tags": tags or [],
            },
        )

    @mcp.tool()
    def briefing_list(
        status: str = "pending", project_id: str = "", tag: str = ""
    ) -> dict[str, Any]:
        """List Leader Briefing items. Default shows pending items for user review.

        Each item carries project_id and tags, so a long decision queue can be
        narrowed to one project and/or one topic.

        Args:
            status: Filter by status: pending / resolved / dismissed / all
            project_id: Restrict to one project. Empty (default) lists every
                project's items — a decision inbox must not hide anything by
                default, and pre-2026-07-27 rows carry no project stamp at all.
                Pass "current" for the project this session is working in.
            tag: Restrict to items carrying this exact tag
        """
        params: list[str] = []
        if status:
            params.append(f"status={urllib.parse.quote(status)}")
        resolved_project = _resolve_project_id("") if project_id == "current" else project_id
        if resolved_project:
            params.append(f"project_id={urllib.parse.quote(resolved_project)}")
        if tag:
            params.append(f"tag={urllib.parse.quote(tag)}")
        qs = f"?{'&'.join(params)}" if params else ""
        return _api_call("GET", f"/api/leader-briefings{qs}")

    @mcp.tool()
    def briefing_resolve(briefing_id: str, resolution: str) -> dict[str, Any]:
        """Resolve a Leader Briefing item with user's decision.

        Args:
            briefing_id: Briefing item ID
            resolution: User's decision text
        """
        return _api_call(
            "PUT",
            f"/api/leader-briefings/{briefing_id}/resolve",
            {"resolution": resolution},
        )

    @mcp.tool()
    def briefing_dismiss(briefing_id: str) -> dict[str, Any]:
        """Dismiss a Leader Briefing item (no action needed).

        Args:
            briefing_id: Briefing item ID

        Returns:
            Updated briefing
        """
        return _api_call("PUT", f"/api/leader-briefings/{briefing_id}/dismiss")
