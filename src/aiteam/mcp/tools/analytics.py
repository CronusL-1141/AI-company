"""Analytics and monitoring MCP tools."""

from __future__ import annotations

import urllib.parse
from typing import Any

from aiteam.mcp._base import _api_call


def register(mcp):
    """Register all analytics-related MCP tools."""

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

    @mcp.tool()
    def usage_attribution(
        scope: str = "",
        scope_id: str = "",
        population: str = "subagent",
        days: int = 0,
    ) -> dict[str, Any]:
        """Report token usage together with how much of it can actually be accounted for.

        Read-only. Every token number comes back alongside its denominator
        (dispatches_total) and its metric label, because a token count without
        those two is meaningless: this repo carries two orthogonal metrics that
        measure 5-25x apart, and sub-agent usage coverage is currently far below
        100%. There is deliberately no total field — 95.6% of the four layers is
        cache_read, so a lone total is just a cache-read count in disguise.

        Args:
            scope: Attribution level — project / session / workflow_run / agent /
                   task. Leave empty to get the coverage matrix (all dispatch
                   paths plus per-hop link coverage) instead of one scope's usage.
            scope_id: ID at that level. Empty means "do not filter on this
                      dimension", i.e. aggregate across the whole ledger.
            population: Dispatch path — "subagent" or "leader_session". These are
                        never merged: one leader session can outweigh every
                        sub-agent combined, which would drown the sub-agent numbers.
            days: Look-back window in days, counted on row creation time. 0 means
                  all history. Never windowed on measurement time — that would
                  drop unmeasured rows out of the denominator and pin coverage
                  at 100%.

        Returns:
            Without scope: coverage matrix rows (dispatches, measured, metric,
            unattributed reasons) plus C_hop per link in the attribution chain.
            With scope: the four token layers, numerator, denominator,
            unattributed breakdown, measured window, and method.
        """
        if not scope:
            return _api_call("GET", f"/api/usage/coverage?days={days}")
        query = urllib.parse.urlencode(
            {
                "scope": scope,
                "scope_id": scope_id,
                "population": population,
                "days": days,
            }
        )
        return _api_call("GET", f"/api/usage/attribution?{query}")
