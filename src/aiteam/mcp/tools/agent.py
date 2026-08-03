"""Agent management MCP tools."""

from __future__ import annotations

import os
import urllib.parse
from typing import Any

from aiteam.mcp._base import _api_call, _resolve_project_id, _resolve_team_id, logger
from aiteam.mcp.tools.views import (
    ACTIVITY_COMPACT_CAP,
    ACTIVITY_HINT,
    AGENT_LIST_HINT,
    FIELDS_ERROR,
    OFFLINE_PREVIEW_DEFAULT,
    REUSE_HINT,
    ROSTER_FETCH_LIMIT,
    compact_activity_row,
    compact_reuse_candidate_row,
    page,
    project_roster,
    resolve_view,
)

# ── 项目类型 → 建议编制（原 team_setup_guide，2026-07-27 并入本模块）───────────
# 定位由"工具"降为"种子"：它是一张静态字典，回答不了"现在装了哪些模板"，
# 独占一个工具名不值当。现在只在 agent_template_recommend(task_type=...) 命中
# 项目类型时作为附加建议返回，真正的模板清单仍来自活体目录扫描。
# 模板名已按活体列表核对（~/.claude/agents 25 个 + 项目级 .claude/agents），
# 不再一律推 team-member 泛用模板——有专职模板就推专职的。
_PROJECT_TYPE_ROLES: dict[str, dict[str, Any]] = {
    "web-app": {
        "description": "全栈Web应用项目",
        "roles": [
            {"name": "tech-lead", "count": 1, "description": "架构设计、技术决策、代码审查",
             "template": "management-tech-lead"},
            {"name": "backend-engineer", "count": "1-2", "description": "API开发、数据库设计、业务逻辑",
             "template": "engineering-backend-architect"},
            {"name": "frontend-engineer", "count": "1-2", "description": "UI组件、页面交互、响应式布局",
             "template": "engineering-frontend-developer"},
            {"name": "qa-engineer", "count": 1, "description": "端到端测试、跨浏览器兼容性",
             "template": "testing-qa-engineer"},
        ],
    },
    "api-service": {
        "description": "后端API服务项目",
        "roles": [
            {"name": "tech-lead", "count": 1, "description": "API架构、接口规范、性能优化",
             "template": "management-tech-lead"},
            {"name": "backend-engineer", "count": "2-3", "description": "端点开发、中间件、数据持久化",
             "template": "engineering-backend-architect"},
            {"name": "api-tester", "count": 1, "description": "API测试、负载测试、契约测试",
             "template": "testing-api-tester"},
        ],
    },
    "data-pipeline": {
        "description": "数据处理管道项目",
        "roles": [
            {"name": "tech-lead", "count": 1, "description": "管道架构、数据流设计",
             "template": "management-tech-lead"},
            {"name": "data-engineer", "count": "1-2", "description": "ETL开发、查询与索引优化",
             "template": "engineering-database-optimizer"},
            {"name": "backend-engineer", "count": "1-2", "description": "调度接入、服务化封装",
             "template": "engineering-backend-architect"},
            {"name": "qa-engineer", "count": 1, "description": "数据质量验证、回归测试",
             "template": "testing-qa-engineer"},
        ],
    },
    "library": {
        "description": "可复用库/SDK项目",
        "roles": [
            {"name": "architect", "count": 1, "description": "API设计、版本策略、兼容性",
             "template": "engineering-software-architect"},
            {"name": "developer", "count": "1-2", "description": "核心实现",
             "template": "engineering-backend-architect"},
            {"name": "technical-writer", "count": 1, "description": "文档与示例编写",
             "template": "support-technical-writer"},
            {"name": "qa-engineer", "count": 1, "description": "单元测试、集成测试、示例验证",
             "template": "testing-qa-engineer"},
        ],
    },
    "refactor": {
        "description": "代码重构项目",
        "roles": [
            {"name": "tech-lead", "count": 1, "description": "重构策略、影响分析、渐进式迁移",
             "template": "management-tech-lead"},
            {"name": "code-reviewer", "count": 1, "description": "代码迁移审查、规范一致性",
             "template": "engineering-code-reviewer"},
            {"name": "qa-engineer", "count": 1, "description": "回归测试、行为一致性验证",
             "template": "testing-qa-engineer"},
        ],
    },
    "bugfix": {
        "description": "Bug修复项目",
        "roles": [
            {"name": "bug-fixer", "count": "1-2", "description": "问题定位、最小化修复",
             "template": "testing-bug-fixer"},
            {"name": "qa-engineer", "count": 1, "description": "复现验证、回归测试",
             "template": "testing-qa-engineer"},
        ],
    },
}

_TEAM_SHAPE_TIP = (
    "编制仅是起点，不必套用：subagent_type 可用现成模板名，也可用 general-purpose "
    "配自定义 prompt 完全自组角色；人数按任务增删，模板文件本身也可随时改或新增。"
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
    def agent_list(
        team_id: str,
        fields: str = "compact",
        include_offline: bool = False,
        limit: int = 50,
        offset: int = 0,
        offline_preview: int = OFFLINE_PREVIEW_DEFAULT,
    ) -> dict[str, Any]:
        """List a team's members - live roster first, offline history on request.

        Default response is a COMPACT projection (view="compact" + hint - it is a
        trimmed view, NOT missing fields). Each member row keeps id / name / role /
        status / an 80-char current_task excerpt / last_active_at; system_prompt,
        config, the context watermark and the token ledger are omitted and come
        back with fields="all".

        Offline members are folded into a count plus a short most-recent digest.
        An offline agent is a terminated process - it cannot be messaged and
        cannot be assigned work - and on the real 51-member session team those
        rows were 96.4% of the payload, which is what made this tool exceed the
        MCP result ceiling and fail outright. Nothing is deleted: the count is
        always reported and include_offline=True returns the full history.

        Args:
            team_id: Team ID or name
            fields: "compact" (default, trimmed rows) / "all" (full agent rows)
            include_offline: Include offline members as full rows instead of a
                count plus digest (default False)
            limit: Max member rows to return after the offline split (default 50,
                capped at 200)
            offset: Pagination offset into the member rows (default 0)
            offline_preview: How many most-recent offline members to show in the
                digest (default 5; ignored when include_offline is True)

        Returns:
            agents (projected rows), total / counted, status_counts, offline
            digest, paging flags, plus view + hint self-identification
        """
        view = resolve_view(fields)
        if view is None:
            return {"success": False, "error": FIELDS_ERROR}
        result = _api_call("GET", f"/api/teams/{team_id}/agents?limit={ROSTER_FETCH_LIMIT}")
        # 错误响应原样透传——投影只作用在成功的名册上。
        if not isinstance(result, dict) or "data" not in result:
            return result
        roster = project_roster(
            result.get("data") or [],
            include_offline=include_offline,
            offline_preview=offline_preview,
            view=view,
        )
        rows, size, has_more = page(roster["members"], limit, offset)
        out: dict[str, Any] = {
            "team_id": team_id,
            "total": result.get("total", roster["counted"]),
            "counted": roster["counted"],
            "status_counts": roster["status_counts"],
            "agents": rows,
            "limit": size,
            "offset": max(0, int(offset or 0)),
            "has_more": has_more,
            "view": view,
            "hint": AGENT_LIST_HINT,
        }
        if "offline" in roster:
            out["offline"] = roster["offline"]
        # 名册本身被上游 200 行帽子截断时明说，别让计数看着像全量。
        if out["counted"] < (out["total"] or 0):
            out["counts_partial"] = (
                f"上游一次最多返回 {ROSTER_FETCH_LIMIT} 行，计数只覆盖前 {out['counted']} 名成员"
            )
        return out

    @mcp.tool()
    def agent_template_list() -> dict[str, Any]:
        """List every Agent template CC can actually resolve.

        Scans all three template sources with CC's own precedence — project-level
        `<project>/.claude/agents/` > user-level `~/.claude/agents/` > the shipped
        `plugin/agents/` — and de-duplicates by filename, so the count matches what
        `subagent_type` will really accept. Each entry carries a `source` field.

        Returns:
            templates: All templates (each with source: project/user/plugin)
            grouped: Templates grouped by category
            total: Total template count
            sources: Per-source counts and scanned directories
        """
        return _api_call("GET", "/api/agent-templates")

    @mcp.tool()
    def agent_template_recommend(task_type: str = "", keywords: str = "") -> dict[str, Any]:
        """Recommend Agent templates — and, for a known project type, a team shape.

        Two layers in one answer:
        1. `recommendations` — live template match against the installed template
           dirs (project > user > plugin), ranked by relevance.
        2. `team_composition` — when task_type names a project type
           (web-app / api-service / data-pipeline / library / refactor / bugfix),
           a suggested role lineup with counts and the template to use for each.
           This is a static seed, not a live probe; it only suggests a shape.

        Args:
            task_type: Task type or project type, e.g., "backend", "frontend",
                "web-app", "api-service", "data-pipeline", "library",
                "refactor", "bugfix"
            keywords: Keywords, space-separated, e.g., "python api database"

        Returns:
            recommendations: Up to 5 matching templates sorted by relevance
            query: Actual query string used
            team_composition: Role lineup seed (only when task_type is a project type)
            project_types: All project types that carry a lineup seed
        """
        params = urllib.parse.urlencode({"task_type": task_type, "keywords": keywords})
        result = _api_call("GET", f"/api/agent-templates/recommend?{params}")
        if not isinstance(result, dict):
            return result
        seed = _PROJECT_TYPE_ROLES.get((task_type or "").strip().lower())
        if seed is not None:
            result["team_composition"] = {
                "project_type": task_type.strip().lower(),
                "description": seed["description"],
                "recommended_roles": seed["roles"],
                "tip": _TEAM_SHAPE_TIP,
            }
        result["project_types"] = list(_PROJECT_TYPE_ROLES)
        return result

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
        NAME — SendMessage(to=...) takes a teammate name and keeps working after
        the agent completes; each candidate's resume_hint is a ready-to-run call
        (with the required `summary`). The raw agentId is the documented fallback
        for nameless rows or when a newer agent took the name.

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
        fields: str = "compact",
    ) -> dict[str, Any]:
        """Query Agent activity records for a team.

        Returns recent activity log entries sorted by timestamp descending,
        including tool name, duration_ms, and an I/O summary.

        Default response is a COMPACT projection (view="compact" + hint - trimmed,
        NOT missing fields): input/output summaries are excerpted because the raw
        output_summary often holds a whole command transcript (a 60-row window
        measured 43.9k chars, right at the MCP result ceiling). Full records via
        fields="all". The compact window is capped at 50 rows - narrow with
        agent_id rather than widening limit.

        Args:
            team_id: Team ID or name (optional, auto-uses active team if empty)
            agent_id: Filter by a specific Agent ID (optional, returns all agents if empty)
            limit: Maximum number of records to return, default 20 (compact view
                caps it at 50)
            fields: "compact" (default, excerpted I/O) / "all" (full records)

        Returns:
            Activity list with tool, timestamp, duration_ms and I/O excerpts;
            compact view adds view + hint self-identification
        """
        view = resolve_view(fields)
        if view is None:
            return {"success": False, "error": FIELDS_ERROR}
        resolved = _resolve_team_id(team_id)
        if not resolved:
            return {"success": False, "error": "未找到活跃团队，请提供 team_id 或先创建团队"}
        wanted = max(1, int(limit or 1))
        effective = min(wanted, ACTIVITY_COMPACT_CAP) if view == "compact" else wanted
        params: list[str] = [f"limit={effective}"]
        if agent_id:
            params.append(f"agent_id={urllib.parse.quote(agent_id)}")
        qs = "?" + "&".join(params)
        result = _api_call("GET", f"/api/teams/{resolved}/activities{qs}")
        if view == "all" or not isinstance(result, dict) or "data" not in result:
            return result
        out: dict[str, Any] = {
            "activities": [compact_activity_row(a) for a in result.get("data") or [] if isinstance(a, dict)],
            "total": result.get("total"),
            "limit": effective,
            "view": "compact",
            "hint": ACTIVITY_HINT,
        }
        if effective < wanted:
            out["limit_capped"] = (
                f"compact 视图一次最多 {ACTIVITY_COMPACT_CAP} 条（请求了 {wanted}）；"
                "要更长的窗口请按 agent_id 收窄，或自担体积用 fields=\"all\""
            )
        return out
