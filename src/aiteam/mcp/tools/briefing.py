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
        """Park a decision for the user while the user is NOT in the conversation.

        Only for questions that come up when nobody can answer them now:
        autonomous /loop work, background workflows, another session's findings,
        or a sub-agent's report that leaves something "for the user to decide".
        If the user is in the conversation, ask them directly instead; the
        result carries ``user_present`` and a ``hint`` when this project saw a
        user message in the last 15 minutes. When the user later answers an
        item, call briefing_resolve on that item right away. Pending items
        expire after 14 days without an answer (status only, never deleted).

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
            status: Filter by status: pending / resolved / dismissed /
                expired (pending for 14 days without an answer) / all
            project_id: Empty (default) lists this session's project plus
                items that carry no project (hooks and background checks
                raise those); in an unregistered directory, every project's
                items. A project id, or "current" for this session's
                project, lists only items stamped with that project.
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
        """Record the user's answer to a pending decision and close it.

        Call it the moment the user answers, in whatever session that happens,
        one call per answered item. The answer is also written as a decision
        event (decision.briefing_resolved), so it stays findable as a decision.

        Args:
            briefing_id: Briefing item ID
            resolution: The user's decision, in the user's own words
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
