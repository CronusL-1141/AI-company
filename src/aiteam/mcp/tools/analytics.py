"""Analytics and monitoring MCP tools."""

from __future__ import annotations

import urllib.parse
from typing import Any

from aiteam.mcp._base import _api_call


def register(mcp):
    """Register all analytics-related MCP tools."""

    @mcp.tool()
    def token_costs(group_by: str = "agent", days: int = 7) -> dict[str, Any]:
        """Query token consumption and cost analytics.

        Returns aggregated token usage and USD cost, sorted by total cost descending.

        Args:
            group_by: Grouping dimension — "agent" (default), "team", or "task"
            days: Look-back window in days (1–90, default 7)

        Returns:
            List of cost records with total_tokens_input, total_tokens_output,
            total_tokens, total_cost_usd, and activity_count per group.
        """
        params = f"?group_by={urllib.parse.quote(group_by)}&days={days}"
        return _api_call("GET", f"/api/analytics/token-costs{params}")

    @mcp.tool()
    def budget_status() -> dict[str, Any]:
        """Check current cost budget utilization.

        Returns weekly spend versus the configured budget, utilization percentage,
        current status (ok / warning / exceeded), and an end-of-week forecast.

        Returns:
            Dict with spent_usd, budget_usd, utilization_pct, status,
            forecast_usd, days_elapsed, and alert_threshold_pct.
        """
        return _api_call("GET", "/api/analytics/budget")

    @mcp.tool()
    def decision_log(
        team_id: str = "",
        event_type: str = "decision",
        limit: int = 20,
    ) -> dict[str, Any]:
        """Query team decision log — task assignments, approach selections, Agent scheduling decisions.

        Args:
            team_id: Team ID (empty string to query all teams)
            event_type: Event type or prefix, e.g., "decision", "decision.task_assigned",
                        "knowledge", "intent". Default "decision" returns all decision events.
            limit: Maximum number of results (default 20, max 200)

        Returns:
            Dict containing a decision event list, sorted by time descending.
            Each event's data field contains:
            - rationale: Decision rationale
            - alternatives: Alternative options list
            - outcome: Decision outcome (pending/success/failed)
        """
        params: list[str] = [f"limit={limit}"]
        if team_id:
            params.append(f"team_id={urllib.parse.quote(team_id)}")
        if event_type:
            type_param = event_type if "." in event_type else f"{event_type}."
            params.append(f"type={urllib.parse.quote(type_param)}")
        query = "&".join(params)
        return _api_call("GET", f"/api/decisions?{query}")

    @mcp.tool()
    def prompt_version_list(template_name: str = "") -> dict[str, Any]:
        """List tracked Agent template versions and usage counts.

        Shows which template versions (identified by content hash) have been used,
        when they were first used, and how many times each version was invoked.

        Args:
            template_name: Optional template name filter (e.g. "engineering-backend-architect").
                           Leave empty to list all tracked templates.

        Returns:
            Dict with "templates" list, each containing template_name, versions (hash + first_used_at
            + usage_count), and total_usage.
        """
        params = f"?template_name={urllib.parse.quote(template_name)}" if template_name else ""
        return _api_call("GET", f"/api/prompt-registry/versions{params}")

    @mcp.tool()
    def prompt_effectiveness(template_name: str = "") -> dict[str, Any]:
        """Return effectiveness statistics for Agent templates.

        Aggregates activity records to compute success rate, average duration,
        and top failure reasons per template. Also shows how many failure alchemy
        lessons are associated with each template.

        Use this to identify which Agent templates perform well and which need
        prompt improvement.

        Args:
            template_name: Optional filter (e.g. "engineering-backend-architect").
                           Leave empty to return stats for all templates.

        Returns:
            Dict with "effectiveness" list containing per-template stats:
            total_activities, success_count, failure_count, success_rate_pct,
            avg_duration_ms, top_failure_reasons, failure_lesson_count.
        """
        params = f"?template_name={urllib.parse.quote(template_name)}" if template_name else ""
        return _api_call("GET", f"/api/prompt-registry/effectiveness{params}")
