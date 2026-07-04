"""Workflow observability MCP tools (I3a — database-backed via API).

让 Leader / workflow agent 会话内查 CC ultracode/Workflow 运行状态，并手动把完成态
富数据拉进 OS（应对 OS 曾离线）。回写台账继续复用既有 task_memo_add / report_save，
不新造回写工具。
"""

from __future__ import annotations

from typing import Any

from aiteam.mcp._base import _api_call


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
    def workflow_get(wf_id: str, include_agents: bool = True) -> dict[str, Any]:
        """Get a Workflow run's full archive (totals + summary/result + per-agent telemetry).

        Args:
            wf_id: Workflow run id (e.g. "wf_8e92fe01-67c").
            include_agents: When True, also fetch the per-agent telemetry rows.

        Returns:
            dict with success flag, the run archive, and (optionally) an "agents" list.
        """
        run = _api_call("GET", f"/api/workflows/{wf_id}")
        if not isinstance(run, dict) or not run.get("wf_id"):
            return run or {"success": False, "error": "Workflow run not found"}

        out: dict[str, Any] = {"success": True, "run": run}
        if include_agents:
            agents_resp = _api_call("GET", f"/api/workflows/{wf_id}/agents")
            if isinstance(agents_resp, dict) and "data" in agents_resp:
                out["agents"] = agents_resp.get("data", [])
                out["agent_total"] = agents_resp.get("total", 0)
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
