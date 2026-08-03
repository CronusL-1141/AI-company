"""Workflow observability MCP tools (I3a — database-backed via API).

让 Leader / workflow agent 会话内查 CC ultracode/Workflow 运行状态，并手动把完成态
富数据拉进 OS（应对 OS 曾离线）。回写台账继续复用既有 task_memo_add / report_save，
不新造回写工具。
"""

from __future__ import annotations

from typing import Any

from aiteam.mcp._base import _api_call
from aiteam.mcp.tools.views import (
    FIELDS_ERROR,
    WORKFLOW_GET_HINT,
    compact_wf_agent_row,
    excerpt,
    resolve_view,
)

# 一次运行的 agent 遥测行上限（compact 视图）。实测一支 166 agent 的运行整档
# 268,753 字符——远超 MCP 单次结果上限，而单看某一个 agent 的完整档案本就该走
# fields="all" 或 workflow 台账页，不该由默认视图承担。
_WF_AGENT_CAP = 40


def register(mcp):
    """Register all workflow observability MCP tools."""

    @mcp.tool(meta={"anthropic/maxResultSizeChars": 500000})
    def workflow_list(
        status: str = "",
        project_id: str = "",
        limit: int = 20,
    ) -> dict[str, Any]:
        """List CC ultracode/Workflow runs tracked by the OS observability layer.

        Args:
            status: Filter by status: "planned" / "running" / "completed" / "interrupted" (empty = all).
            project_id: Filter by project ID (empty = all projects).
            limit: Maximum number of runs to return (default 20).

        Returns:
            dict with success flag and a "runs" list (wf_id/name/status/agent counts/tokens/duration).
        """
        params: list[str] = [f"limit={limit}"]
        if status:
            params.append(f"status={status}")
        if project_id:
            params.append(f"project_id={project_id}")
        qs = "&".join(params)

        result = _api_call("GET", f"/api/workflows?{qs}")
        if isinstance(result, dict) and "data" in result:
            runs = result.get("data", [])
            return {
                "success": True,
                "runs": [
                    {
                        "wf_id": r.get("wf_id", ""),
                        "name": r.get("name", ""),
                        "status": r.get("status", ""),
                        "source": r.get("source", ""),
                        "planned_agent_count": r.get("planned_agent_count", 0),
                        "agent_count": r.get("agent_count", 0),
                        "total_tokens": r.get("total_tokens", 0),
                        "total_tool_calls": r.get("total_tool_calls", 0),
                        "duration_ms": r.get("duration_ms"),
                        "completed_at": r.get("completed_at"),
                    }
                    for r in runs
                ],
                "total": result.get("total", len(runs)),
            }
        return result or {"success": False, "error": "API call failed"}

    @mcp.tool(meta={"anthropic/maxResultSizeChars": 500000})
    def workflow_get(
        wf_id: str,
        include_agents: bool = True,
        fields: str = "compact",
        limit: int = _WF_AGENT_CAP,
    ) -> dict[str, Any]:
        """Get a Workflow run's archive (totals + summary/result + per-agent telemetry).

        Default response is a COMPACT projection (view="compact" + hint - trimmed,
        NOT missing fields). A big run's full archive does not fit: a real
        166-agent run measured 268,753 chars, of which prompt_preview and
        result_preview alone were 53%. Compact keeps every scalar on the run,
        excerpts its result/summary, and projects the agent rows down to
        identity / phase / cost / state plus the os_agent_id drill-down key.

        Args:
            wf_id: Workflow run id (e.g. "wf_8e92fe01-67c").
            include_agents: When True, also fetch the per-agent telemetry rows.
            fields: "compact" (default, trimmed rows) / "all" (full archive)
            limit: Max agent rows to return in compact view (default 40)

        Returns:
            dict with success flag, the run archive, agent_totals, and
            (optionally) an "agents" list; compact adds view + hint.
        """
        view = resolve_view(fields)
        if view is None:
            return {"success": False, "error": FIELDS_ERROR}
        run = _api_call("GET", f"/api/workflows/{wf_id}")
        if not isinstance(run, dict) or not run.get("wf_id"):
            return run or {"success": False, "error": "Workflow run not found"}

        if view == "compact":
            # 大文本降级为摘要（不删除）：result 是运行的最终产出，summary 是一句话结论。
            run = dict(run)
            run["result"] = excerpt(run.get("result"), 400)
            run["summary"] = excerpt(run.get("summary"), 200)
        out: dict[str, Any] = {"success": True, "run": run}
        if view == "compact":
            out["view"] = "compact"
            out["hint"] = WORKFLOW_GET_HINT
        if not include_agents:
            return out

        agents_resp = _api_call("GET", f"/api/workflows/{wf_id}/agents")
        if not isinstance(agents_resp, dict) or "data" not in agents_resp:
            return out
        rows = [a for a in (agents_resp.get("data") or []) if isinstance(a, dict)]
        out["agent_total"] = agents_resp.get("total", len(rows))
        if view == "all":
            out["agents"] = rows
            return out
        states: dict[str, int] = {}
        for row in rows:
            key = str(row.get("state") or "unknown")
            states[key] = states.get(key, 0) + 1
        out["agent_totals"] = {
            "by_state": states,
            "tokens": sum(int(r.get("tokens") or 0) for r in rows),
            "tool_calls": sum(int(r.get("tool_calls") or 0) for r in rows),
        }
        size = max(1, min(int(limit or 1), 200))
        out["agents"] = [compact_wf_agent_row(r) for r in rows[:size]]
        if len(rows) > size:
            out["agents_omitted"] = (
                f"另有 {len(rows) - size} 个 agent 行未展开（compact 视图一次 {size} 行）；"
                '全量用 fields="all"，或按 os_agent_id 单点查'
            )
        return out

    @mcp.tool()
    def workflow_reconcile(
        project_dir: str = "",
        session_id: str = "",
    ) -> dict[str, Any]:
        """Reconcile finished Workflow runs from disk into the OS (repair after OS was offline).

        Scans ``~/.claude/projects/<slug>/*/workflows/wf_*.json`` and ingests each run's
        full telemetry (tokens/duration/per-agent). Idempotent — safe to re-run.

        Args:
            project_dir: Limit the scan to the project owning this directory (empty = all projects).
            session_id: Limit the scan to a single CC session's workflows (empty = all sessions).

        Returns:
            dict with success flag and ingested/updated/errors/scanned counts.
        """
        payload: dict[str, Any] = {}
        if project_dir:
            payload["project_dir"] = project_dir
        if session_id:
            payload["session_id"] = session_id

        result = _api_call("POST", "/api/workflows/reconcile", payload)
        return result or {"success": False, "error": "API call failed"}
