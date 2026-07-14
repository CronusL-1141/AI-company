"""Agent management MCP tools."""

from __future__ import annotations

import os
import urllib.parse
from typing import Any

from aiteam.mcp._base import _api_call, _resolve_project_id, _resolve_team_id, logger
from aiteam.mcp.tools.views import (
    FIELDS_ERROR,
    REUSE_HINT,
    compact_reuse_candidate_row,
    resolve_view,
)


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
        model: str = "",
        system_prompt: str = "",
    ) -> dict[str, Any]:
        """⚠️ INTERNAL USE ONLY — 请使用CC原生的Agent工具创建Agent，不要调用此MCP工具。

        NOTE: For normal workflow, use CC's Agent tool with team_name parameter instead.
        CC Agent tool spawns a real subprocess AND auto-registers via hooks.
        This MCP tool only creates a DB record — no actual agent process is started.

        Args:
            team_id: Target team ID or name
            name: Agent name
            role: Agent role description
            model: Model to use; empty = unknown (displayed as --, backfilled by telemetry)
            system_prompt: Agent's system prompt

        Returns:
            Agent info
        """
        effective_prompt = system_prompt
        if not effective_prompt:
            effective_prompt = _render_agent_prompt(role)

        result = _api_call(
            "POST",
            f"/api/teams/{team_id}/agents",
            {
                "name": name,
                "role": role,
                "model": model,
                "system_prompt": effective_prompt,
            },
        )
        result["_warning"] = "此工具仅创建DB记录不启动真实进程。请使用CC原生TeamCreate+Agent工具。"
        return result

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
    def agent_reuse_recommend(
        query: str = "",
        keywords: str = "",
        project_id: str = "",
        session_id: str = "",
        limit: int = 10,
        fields: str = "compact",
    ) -> dict[str, Any]:
        """Recommend whether to reuse an existing sub-agent for a follow-up task.

        For follow-up work (bug re-fix, deeper research, same-domain iteration),
        resuming a prior sub-agent preserves its accumulated context. This tool
        ranks prior sub-agents by same-domain match, reads their P1 context
        watermark, infers reachability, and recommends one of three actions:
        reuse (SendMessage resumes it) / slim_then_reuse (self-summarize then spawn
        fresh with the summary) / spawn_new. It only recommends; the Leader decides.

        Availability tiers: live (same session, reachable now) / resumable (same
        session, offline but transcript fresh) / cross-session (another session,
        needs claude --resume) / expired (past retention). Address candidates by
        cc_tool_use_id (agentId), not name (a re-spawned agent may reuse the name).

        Default response is a COMPACT projection (view="compact" + hint — trimmed,
        NOT missing fields): decision signals and call keys kept, full rationale and
        watermark detail via fields="all".

        Args:
            query: The follow-up task description / target domain
            keywords: Extra space-separated keywords to widen domain matching
            project_id: Scope to a project (optional; defaults to the active project,
                empty searches all teams)
            session_id: The caller's CC session id (optional; enables precise
                cross-session detection, otherwise availability is inferred from status)
            limit: Max candidates to return (default 10)
            fields: "compact" (default, trimmed projection) / "all" (full rows)

        Returns:
            candidates (ranked), default_recommendation (reuse/slim_then_reuse/
            spawn_new), query; compact view adds view + hint self-identification
        """
        view = resolve_view(fields)
        if view is None:
            return {"success": False, "error": FIELDS_ERROR}
        resolved_project = _resolve_project_id(project_id)  # optional — "" searches all teams
        params: list[str] = [f"limit={limit}"]
        if resolved_project:
            params.append(f"project_id={urllib.parse.quote(resolved_project)}")
        if query:
            params.append(f"query={urllib.parse.quote(query)}")
        if keywords:
            params.append(f"keywords={urllib.parse.quote(keywords)}")
        if session_id:
            params.append(f"session_id={urllib.parse.quote(session_id)}")
        qs = "?" + "&".join(params)
        result = _api_call("GET", f"/api/agents/reuse-recommend{qs}")
        # Projection only on success; errors / full view pass through unchanged.
        if view == "all" or not isinstance(result, dict) or "candidates" not in result:
            return result
        return {
            "candidates": [
                compact_reuse_candidate_row(c) for c in result.get("candidates") or []
            ],
            "default_recommendation": result.get("default_recommendation"),
            "query": result.get("query"),
            "view": "compact",
            "hint": REUSE_HINT,
        }

    @mcp.tool()
    def fleet_dispatch(
        target_session_id: str,
        instruction: str,
        project_id: str = "",
        tools_level: str = "safe",
        max_turns: int = 0,
    ) -> dict[str, Any]:
        """Dispatch an operational instruction to another ship (CC session) in the fleet.

        The fleet down-channel drives an EXISTING idle session to run one turn via
        headless `claude -p --resume` (fleet-layer design §4). Use it to nudge an idle
        ship to advance a task or report its status - NOT to make strategic decisions on
        the user's behalf (the dispatched turn is constrained to operational work).

        Safety gate (enforced server-side, no subprocess spawns until it passes):
        - The target must be RESUMABLE: its transcript file still exists.
        - The target must NOT be user-live: its file must be idle beyond a conservative
          guard (FLEET_DISPATCH_MIN_IDLE_SECONDS, > the 15min live window) so a dispatch
          never competes with someone typing in that session. A too-fresh target is
          refused with availability="live".
        - Dispatches are deduped per-session, share the global wake concurrency limit and
          circuit breaker, and every one is ledgered in wake_sessions.

        Get target_session_id from the fleet view / project summary (each ship's
        session_id). This tool RECOMMENDS nothing and DECIDES nothing strategic; it only
        relays an operational instruction to an idle ship.

        Args:
            target_session_id: The ship's CC session id to resume and dispatch to
            instruction: The operational instruction (advance task X / report status / etc.)
            project_id: Project scope (optional; inferred from the session's agents if empty)
            tools_level: Tool preset for the dispatched turn - "safe" (default) or
                "with_bash" (adds Bash). Never exceeds the requested preset.
            max_turns: Max turns for the dispatched run (0 = server default)

        Returns:
            {success, status, ...}. status is one of: started / refused (with reason +
            availability) / skipped_concurrent / skipped_max_concurrent / fused /
            unresolved_project / unavailable / error_config / error_start.
        """
        if not target_session_id or not target_session_id.strip():
            return {"success": False, "error": "target_session_id is required"}
        if not instruction or not instruction.strip():
            return {"success": False, "error": "instruction is required"}
        payload: dict[str, Any] = {
            "target_session_id": target_session_id.strip(),
            "instruction": instruction,
            "project_id": _resolve_project_id(project_id) or "",
            "tools_level": tools_level or "safe",
        }
        if max_turns and max_turns > 0:
            payload["max_turns"] = max_turns
        return _api_call("POST", "/api/fleet/dispatch", payload)

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
