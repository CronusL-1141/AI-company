"""AI Team OS — Research reports routes."""

from __future__ import annotations

import re
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter(prefix="/api/reports", tags=["reports"])

_BASE_DATA = Path.home() / ".claude" / "data" / "ai-team-os"

# Expected filename pattern: {author}_{topic}_{YYYY-MM-DD}.md
_FILENAME_RE = re.compile(
    r"^(?P<author>.+?)_(?P<topic>.+?)_(?P<date>\d{4}-\d{2}-\d{2})\.md$"
)


def _get_reports_dir() -> Path:
    """Return the global reports directory."""
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
    for path in reports_dir.iterdir():
        if not path.is_file():
            continue
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

    # Try multiple encodings to handle files from different sources
    raw = path.read_bytes()
    content = None
    for enc in ("utf-8", "gbk", "gb2312", "latin-1"):
        try:
            content = raw.decode(enc)
            break
        except (UnicodeDecodeError, LookupError):
            continue
    if content is None:
        content = raw.decode("utf-8", errors="replace")
    return ReportDetail(
        filename=filename,
        author=meta["author"],
        topic=meta["topic"],
        date=meta["date"],
        content=content,
    )
