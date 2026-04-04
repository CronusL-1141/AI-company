"""Report storage MCP tools (filesystem-based, no FastAPI)."""

from __future__ import annotations

import os
import pathlib
from datetime import date
from typing import Any

from aiteam.mcp._base import _resolve_project_id

_REPORTS_GLOBAL = pathlib.Path.home() / ".claude" / "data" / "ai-team-os" / "reports"


def _get_reports_dir() -> pathlib.Path:
    """Return the reports directory for the current project context.

    When CLAUDE_PROJECT_DIR env var is set, derives a 12-char md5 project key
    from the resolved path and returns:
        ~/.claude/data/ai-team-os/projects/{md5_key}/reports/

    Falls back to the global path when env var is not set:
        ~/.claude/data/ai-team-os/reports/
    """
    import hashlib

    project_dir = os.environ.get("CLAUDE_PROJECT_DIR", "")
    if project_dir:
        normalized = str(pathlib.Path(project_dir).resolve())
        project_key = hashlib.md5(normalized.encode()).hexdigest()[:12]
        return (
            pathlib.Path.home()
            / ".claude"
            / "data"
            / "ai-team-os"
            / "projects"
            / project_key
            / "reports"
        )
    return _REPORTS_GLOBAL


def _ensure_reports_dir() -> pathlib.Path:
    """Create the reports directory if it does not exist and return its path."""
    reports_dir = _get_reports_dir()
    reports_dir.mkdir(parents=True, exist_ok=True)
    return reports_dir


def _parse_report_filename(filename: str) -> dict[str, str]:
    """Parse author/topic/date from a report filename.

    Expected format: {author}_{topic}_{YYYY-MM-DD}.md
    Returns dict with keys author, topic, date (all empty string on parse failure).
    """
    stem = filename.removesuffix(".md")
    parts = stem.rsplit("_", 1)
    if len(parts) != 2:
        return {"author": "", "topic": "", "date": ""}
    date_str = parts[1]
    if len(date_str) != 10 or date_str.count("-") != 2:
        return {"author": "", "topic": "", "date": ""}
    remainder = parts[0]
    sub = remainder.split("_", 1)
    author = sub[0]
    topic = sub[1] if len(sub) > 1 else ""
    return {"author": author, "topic": topic, "date": date_str}


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
        """Save a research/analysis report to the shared reports directory.

        Automatically generates a filename: {author}_{topic}_{YYYY-MM-DD}.md
        and writes it to ~/.claude/data/ai-team-os/projects/{project_id}/reports/ when
        CLAUDE_PROJECT_DIR is set, otherwise to the global ~/.claude/data/ai-team-os/reports/.
        If a file with the same name already exists it will be overwritten.

        Args:
            author: Agent name, e.g. "rd-scanner".
            topic: Topic keywords, e.g. "ai-products-march".
            content: Report body in Markdown format.
            report_type: One of "research" / "design" / "analysis" / "meeting-minutes".
            task_id: Optional task ID to associate this report with a specific task.
            team_id: Optional team ID to associate this report with a specific team.

        Returns:
            dict with success flag, saved file path, and filename.
        """
        reports_dir = _ensure_reports_dir()
        today = date.today().isoformat()
        filename = f"{author}_{topic}_{today}.md"
        filepath = reports_dir / filename

        project_id = _resolve_project_id("")

        header = (
            f"---\n"
            f"author: {author}\n"
            f"topic: {topic}\n"
            f"date: {today}\n"
            f"type: {report_type}\n"
            f"project_id: {project_id}\n"
        )
        if task_id:
            header += f"task_id: {task_id}\n"
        if team_id:
            header += f"team_id: {team_id}\n"
        header += f"---\n\n"

        try:
            filepath.write_text(header + content, encoding="utf-8")
        except OSError as exc:
            return {"success": False, "error": str(exc)}

        return {
            "success": True,
            "filename": filename,
            "path": str(filepath),
            "author": author,
            "topic": topic,
            "date": today,
            "report_type": report_type,
            "project_id": project_id,
        }

    @mcp.tool()
    def report_list(
        author: str = "",
        topic: str = "",
        limit: int = 20,
    ) -> dict[str, Any]:
        """List saved reports, optionally filtered by author or topic keyword.

        Scans the project-scoped reports directory (or global fallback) and returns metadata parsed
        from filenames, sorted newest-first.

        Args:
            author: Filter by exact author name (empty = no filter).
            topic: Filter by topic keyword — matches if keyword appears anywhere in the topic segment (empty = no filter).
            limit: Maximum number of results to return (default 20).

        Returns:
            dict with success flag and a "reports" list of metadata dicts.
        """
        reports_dir = _ensure_reports_dir()
        entries: list[dict[str, str]] = []

        for path in sorted(
            reports_dir.glob("*.md"),
            key=lambda p: _parse_report_filename(p.name)["date"],
            reverse=True,
        ):
            meta = _parse_report_filename(path.name)
            if not meta["date"]:
                continue
            if author and meta["author"] != author:
                continue
            if topic and topic.lower() not in meta["topic"].lower():
                continue
            entries.append(
                {
                    "filename": path.name,
                    "author": meta["author"],
                    "topic": meta["topic"],
                    "date": meta["date"],
                }
            )
            if len(entries) >= limit:
                break

        return {"success": True, "reports": entries, "total": len(entries)}

    @mcp.tool()
    def report_read(filename: str) -> dict[str, Any]:
        """Read the full content of a saved report by filename.

        Args:
            filename: Exact filename, e.g. "rd-scanner_ai-products-march_2026-03-22.md".

        Returns:
            dict with success flag, content string, and metadata (author/topic/date).
        """
        reports_dir = _ensure_reports_dir()
        filepath = reports_dir / filename

        if not filepath.exists():
            return {
                "success": False,
                "error": f"Report not found: {filename}",
                "path": str(filepath),
            }

        try:
            content = filepath.read_text(encoding="utf-8")
        except OSError as exc:
            return {"success": False, "error": str(exc)}

        meta = _parse_report_filename(filename)
        return {
            "success": True,
            "filename": filename,
            "content": content,
            "author": meta["author"],
            "topic": meta["topic"],
            "date": meta["date"],
            "path": str(filepath),
        }
