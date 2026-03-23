"""AI Team OS — Research reports routes."""

from __future__ import annotations

import re
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from aiteam.api.project_context import current_project_id

router = APIRouter(prefix="/api/reports", tags=["reports"])

_BASE_DATA = Path.home() / ".claude" / "data" / "ai-team-os"

# Expected filename pattern: {author}_{topic}_{YYYY-MM-DD}.md
_FILENAME_RE = re.compile(
    r"^(?P<author>.+?)_(?P<topic>.+?)_(?P<date>\d{4}-\d{2}-\d{2})\.md$"
)


def _get_reports_dir() -> Path:
    """Return the project-scoped reports directory for the current request.

    When X-Project-Dir header is present the middleware sets current_project_id,
    so we route to:
        ~/.claude/data/ai-team-os/projects/{project_id}/reports/

    Otherwise fall back to the legacy global path:
        ~/.claude/data/ai-team-os/reports/
    """
    pid = current_project_id.get("")
    if pid:
        return _BASE_DATA / "projects" / pid / "reports"
    return _BASE_DATA / "reports"


class ReportMeta(BaseModel):
    filename: str
    author: str
    topic: str
    date: str
    size_bytes: int


class ReportDetail(BaseModel):
    filename: str
    author: str
    topic: str
    date: str
    content: str


def _parse_filename(filename: str) -> dict[str, str] | None:
    """Parse report filename and extract metadata. Returns None if pattern doesn't match."""
    m = _FILENAME_RE.match(filename)
    if not m:
        return None
    return {
        "author": m.group("author"),
        "topic": m.group("topic"),
        "date": m.group("date"),
    }


@router.get("", response_model=list[ReportMeta])
async def list_reports() -> list[ReportMeta]:
    """List reports for the current project, sorted by date descending."""
    reports_dir = _get_reports_dir()
    if not reports_dir.exists():
        return []

    results: list[ReportMeta] = []
    for path in reports_dir.glob("*.md"):
        meta = _parse_filename(path.name)
        if meta is None:
            # Include files with non-standard names using fallback values
            meta = {"author": "unknown", "topic": path.stem, "date": ""}
        results.append(
            ReportMeta(
                filename=path.name,
                author=meta["author"],
                topic=meta["topic"],
                date=meta["date"],
                size_bytes=path.stat().st_size,
            )
        )

    # Sort by date descending; empty dates go last
    results.sort(key=lambda r: r.date, reverse=True)
    return results


@router.get("/{filename}", response_model=ReportDetail)
async def get_report(filename: str) -> ReportDetail:
    """Read a report file by filename within the current project's reports directory."""
    # Prevent path traversal
    if "/" in filename or "\\" in filename or ".." in filename:
        raise HTTPException(status_code=400, detail="Invalid filename")

    path = _get_reports_dir() / filename
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail=f"Report '{filename}' not found")

    meta = _parse_filename(filename)
    if meta is None:
        meta = {"author": "unknown", "topic": path.stem, "date": ""}

    content = path.read_text(encoding="utf-8")
    return ReportDetail(
        filename=filename,
        author=meta["author"],
        topic=meta["topic"],
        date=meta["date"],
        content=content,
    )
