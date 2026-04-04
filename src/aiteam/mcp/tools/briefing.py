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
    ) -> dict[str, Any]:
        """Add a decision item to Leader Briefing for user review.

        Use when Leader encounters decisions that require user input:
        project direction, architecture choices, budget/resource allocation.

        Args:
            title: Brief description of the decision needed
            description: Detailed context
            options: Available choices (e.g. "A: option1 / B: option2")
            recommendation: Leader's suggested choice and reasoning
            urgency: high / medium / low
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
            },
        )

    @mcp.tool()
    def briefing_list(status: str = "pending") -> dict[str, Any]:
        """List Leader Briefing items. Default shows pending items for user review.

        Args:
            status: Filter by status: pending / resolved / dismissed / all
        """
        qs = f"?status={urllib.parse.quote(status)}" if status else ""
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
