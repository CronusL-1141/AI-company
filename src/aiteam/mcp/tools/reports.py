"""Report management MCP tools (database-backed via API)."""

from __future__ import annotations

from typing import Any

from aiteam.mcp._base import _api_call


def register(mcp):
    """Register all report-related MCP tools."""

    @mcp.tool()
    def report_save(
        author: str,
        topic: str,
        content: str,
        report_type: str = "research",
        task_id: str = "",
        team_id: str = "",
    ) -> dict[str, Any]:
        """Save a research/analysis report to the database.

        Reports are stored in the database with project isolation — no filesystem
        permission needed. Reports appear on the Dashboard reports page automatically.

        Args:
            author: Agent name, e.g. "rd-scanner".
            topic: Topic keywords, e.g. "ai-products-march".
            content: Report body in Markdown format.
            report_type: One of "research" / "design" / "analysis" / "meeting-minutes".
            task_id: Optional task ID to associate this report with a specific task.
            team_id: Optional team ID to associate this report with a specific team.

        Returns:
            dict with success flag, report ID, and metadata.
        """
        payload: dict[str, Any] = {
            "author": author,
            "topic": topic,
            "content": content,
            "report_type": report_type,
        }
        if task_id:
            payload["task_id"] = task_id
        if team_id:
            payload["team_id"] = team_id

        result = _api_call("POST", "/api/reports", payload)
        if result and result.get("id"):
            return {
                "success": True,
                "id": result["id"],
                "filename": result.get("filename", ""),
                "author": result.get("author", author),
                "topic": result.get("topic", topic),
                "date": result.get("date", ""),
                "report_type": result.get("report_type", report_type),
                "project_id": result.get("project_id", ""),
            }
        return result or {"success": False, "error": "API call failed"}

    @mcp.tool()
    def report_list(
        author: str = "",
        topic: str = "",
        report_type: str = "",
        limit: int = 20,
    ) -> dict[str, Any]:
        """List saved reports, optionally filtered by author, topic, or type.

        Returns reports for the current project context, sorted newest-first.

        Args:
            author: Filter by exact author name (empty = no filter).
            topic: Filter by topic keyword (empty = no filter).
            report_type: Filter by type: "research" / "design" / "analysis" / "meeting-minutes" (empty = all).
            limit: Maximum number of results to return (default 20).

        Returns:
            dict with success flag and a "reports" list of metadata dicts.
        """
        params: list[str] = [f"limit={limit}"]
        if author:
            params.append(f"author={author}")
        if topic:
            params.append(f"topic={topic}")
        if report_type:
            params.append(f"report_type={report_type}")
        qs = "&".join(params)

        result = _api_call("GET", f"/api/reports?{qs}")
        if isinstance(result, list):
            return {
                "success": True,
                "reports": [
                    {
                        "id": r.get("id", ""),
                        "filename": r.get("filename", ""),
                        "author": r.get("author", ""),
                        "topic": r.get("topic", ""),
                        "date": r.get("date", ""),
                        "report_type": r.get("report_type", ""),
                    }
                    for r in result
                ],
                "total": len(result),
            }
        return result or {"success": False, "error": "API call failed"}

    @mcp.tool()
    def report_read(report_id: str) -> dict[str, Any]:
        """Read the full content of a saved report by ID.

        Args:
            report_id: Report ID (UUID).

        Returns:
            dict with success flag, content string, and metadata.
        """
        result = _api_call("GET", f"/api/reports/{report_id}")
        if result and result.get("id"):
            return {
                "success": True,
                "id": result["id"],
                "filename": result.get("filename", ""),
                "content": result.get("content", ""),
                "author": result.get("author", ""),
                "topic": result.get("topic", ""),
                "date": result.get("date", ""),
                "report_type": result.get("report_type", ""),
            }
        return result or {"success": False, "error": "Report not found"}
