"""AI Team OS — Research reports routes (database-backed)."""

from __future__ import annotations

from datetime import date
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from aiteam.api.deps import get_repository
from aiteam.storage.repository import StorageRepository
from aiteam.types import Report

router = APIRouter(prefix="/api/reports", tags=["reports"])


class ReportMeta(BaseModel):
    id: str
    filename: str
    author: str
    topic: str
    report_type: str
    date: str
    size_bytes: int
    project_id: str = ""
    task_id: str = ""
    team_id: str = ""


class ReportDetail(ReportMeta):
    content: str


class ReportCreate(BaseModel):
    author: str
    topic: str
    content: str
    report_type: str = "research"
    task_id: str = ""
    team_id: str = ""


def _to_meta(r: Report) -> ReportMeta:
    filename = f"{r.author}_{r.topic}_{r.date}.md"
    return ReportMeta(
        id=r.id,
        filename=filename,
        author=r.author,
        topic=r.topic,
        report_type=r.report_type,
        date=r.date,
        size_bytes=len(r.content.encode("utf-8")),
        project_id=r.project_id,
        task_id=r.task_id,
        team_id=r.team_id,
    )


def _to_detail(r: Report) -> ReportDetail:
    filename = f"{r.author}_{r.topic}_{r.date}.md"
    return ReportDetail(
        id=r.id,
        filename=filename,
        author=r.author,
        topic=r.topic,
        report_type=r.report_type,
        date=r.date,
        size_bytes=len(r.content.encode("utf-8")),
        content=r.content,
        project_id=r.project_id,
        task_id=r.task_id,
        team_id=r.team_id,
    )


def _get_project_id(request: Request) -> str:
    return request.headers.get("X-Project-Id", "")


@router.get("", response_model=list[ReportMeta])
async def list_reports(
    request: Request,
    project_id: str = Query("", description="Filter by project ID (empty = all projects)"),
    report_type: str = Query("", description="Filter by report type"),
    author: str = Query("", description="Filter by author"),
    topic: str = Query("", description="Filter by topic keyword"),
    limit: int = Query(50, ge=1, le=200),
    repo: StorageRepository = Depends(get_repository),
) -> list[ReportMeta]:
    """List reports with optional filtering. When project_id is empty, returns all projects' reports."""
    # Query param takes priority; fall back to X-Project-Id header (for MCP calls)
    pid = project_id or _get_project_id(request)
    reports = await repo.list_reports(
        project_id=pid,
        report_type=report_type,
        author=author,
        topic=topic,
        limit=limit,
    )
    return [_to_meta(r) for r in reports]


@router.get("/{report_id}", response_model=ReportDetail)
async def get_report(
    report_id: str,
    repo: StorageRepository = Depends(get_repository),
) -> ReportDetail:
    """Read a report by ID."""
    report = await repo.get_report(report_id)
    if not report:
        raise HTTPException(status_code=404, detail=f"Report '{report_id}' not found")
    return _to_detail(report)


@router.post("", response_model=ReportDetail, status_code=201)
async def create_report(
    body: ReportCreate,
    request: Request,
    repo: StorageRepository = Depends(get_repository),
) -> ReportDetail:
    """Create a new report."""
    project_id = _get_project_id(request)
    report = Report(
        id=str(uuid4()),
        project_id=project_id,
        author=body.author,
        topic=body.topic,
        report_type=body.report_type,
        date=date.today().isoformat(),
        content=body.content,
        task_id=body.task_id,
        team_id=body.team_id,
    )
    await repo.create_report(report)
    return _to_detail(report)


@router.delete("/{report_id}")
async def delete_report(
    report_id: str,
    repo: StorageRepository = Depends(get_repository),
) -> dict:
    """Delete a report."""
    ok = await repo.delete_report(report_id)
    if not ok:
        raise HTTPException(status_code=404, detail=f"Report '{report_id}' not found")
    return {"success": True, "message": "Report deleted"}
