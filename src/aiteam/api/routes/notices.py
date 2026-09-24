"""AI Team OS — user notice routes (docs/user-notice-design.md §5.5).

Exit hooks call ``POST /api/notices/pending`` and print what comes back; the
Dashboard, ``/os-doctor`` and the notice tools read ``GET /api/notices``.
All handlers are async and keep file IO in worker threads.
"""

from __future__ import annotations

import re
from datetime import timedelta
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from aiteam.api.deps import get_repository
from aiteam.api.language import resolve_language
from aiteam.clock import utc_now
from aiteam.services.notices import ledger
from aiteam.services.notices.catalog import CATALOG, DESIGN_NUMBER, render_entry
from aiteam.services.notices.detectors.registration import dismiss_dir
from aiteam.storage.repository import StorageRepository
from aiteam.types import Notice, NoticeDelivery, NoticeStatus, PendingRequest, PendingResponse

router = APIRouter(prefix="/api/notices", tags=["notices"])

_STATUS_FILTERS = {
    "active": (NoticeStatus.ACTIVE.value, NoticeStatus.SNOOZED.value),
    "all": None,
    "cleared": (NoticeStatus.CLEARED.value,),
    "dismissed": (NoticeStatus.DISMISSED.value,),
    "snoozed": (NoticeStatus.SNOOZED.value,),
}
_ACTION = re.compile(r"「[^」]+」|\"[^\"]+\"")


class NoticeRegisterBody(BaseModel):
    key: str = Field(min_length=1, max_length=512)
    catalog_id: str = Field(min_length=1, max_length=64)
    variant: str = Field(default="", max_length=64)
    params: dict[str, Any] = Field(default_factory=dict)
    project_id: str = Field(default="", max_length=64)
    session_id: str = Field(default="", max_length=256)
    host: Literal["", "cc", "codex"] = ""
    source: str = Field(default="", max_length=100)


def _delivery_summary(delivery: NoticeDelivery | None) -> dict | None:
    if delivery is None:
        return None
    return delivery.model_dump(mode="json", exclude={"id", "key"})


def _summary(notice: Notice, language: str, host: str, last: NoticeDelivery | None) -> dict:
    entry = CATALOG.get(notice.catalog_id)
    host = notice.host or host
    line = ""
    action = ""
    if entry is not None:
        line = render_entry(
            entry, variant=notice.variant, language=language, host=host, params=notice.params,
        ).plain
        match = _ACTION.search(line)
        action = match.group(0) if match else ""
    return {
        "key": notice.key,
        "catalog_id": notice.catalog_id,
        "design_number": DESIGN_NUMBER.get(notice.catalog_id, ""),
        "kind": entry.kind.value if entry else "",
        "color": entry.color.value if entry else "",
        "severity": entry.severity.value if entry else "",
        "status": notice.status.value,
        "variant": notice.variant,
        "user_line": line,
        "action": action,
        "project_id": notice.project_id,
        "session_id": notice.session_id,
        "host": notice.host,
        "source": notice.source,
        "first_seen_at": notice.first_seen_at.isoformat(),
        "last_seen_at": notice.last_seen_at.isoformat(),
        "cleared_at": notice.cleared_at.isoformat() if notice.cleared_at else None,
        "snoozed_until": notice.snoozed_until.isoformat() if notice.snoozed_until else None,
        "last_delivery": _delivery_summary(last),
    }


async def _language(language: str | None) -> str:
    if language in ("zh", "en"):
        return language
    return (await resolve_language(host="system"))["effective"]


@router.post("/pending", response_model=PendingResponse)
async def pending_notices(
    body: PendingRequest,
    repo: StorageRepository = Depends(get_repository),
) -> PendingResponse:
    """Everything one hook exit should show now: rendered lines and model notes.

    Imports the hook's local records, marks reported deliveries as written, runs
    the detectors for this moment, confirms or refires unreliable deliveries,
    then claims at most the budgeted number of notices atomically.
    """
    return await ledger.pending(repo, body)


@router.get("")
async def list_notices(
    status: Literal["active", "all", "cleared", "dismissed", "snoozed"] = "active",
    host: Literal["cc", "codex"] = "cc",
    project_id: str | None = Query(default=None, max_length=64),
    language: str | None = Query(default=None, max_length=8),
    fresh: int = Query(default=0, ge=0, le=1),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    repo: StorageRepository = Depends(get_repository),
) -> dict[str, Any]:
    """Paged notice summaries; ``fresh=1`` runs every detector first."""
    if fresh:
        await ledger.refresh(repo, host=host)
    else:
        await ledger.expire_ttl(repo, utc_now())
    chosen = await _language(language)
    rows, total = await repo.list_notices(
        statuses=_STATUS_FILTERS[status],
        project_ids=None if project_id is None else [project_id],
        limit=limit, offset=offset,
    )
    latest: dict[str, NoticeDelivery] = {}
    if rows:
        for delivery in await repo.list_notice_deliveries(keys=[row.key for row in rows]):
            latest.setdefault(delivery.key, delivery)
    return {
        "items": [_summary(row, chosen, host, latest.get(row.key)) for row in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
        "language": chosen,
    }


@router.post("", response_model=Notice)
async def register_notice(
    body: NoticeRegisterBody,
    repo: StorageRepository = Depends(get_repository),
) -> Notice:
    """Register or refresh one notice by key (catalog-validated)."""
    try:
        return await ledger.register(
            repo, key=body.key, catalog_id=body.catalog_id, variant=body.variant,
            params=body.params, project_id=body.project_id, session_id=body.session_id,
            host=body.host, source=body.source,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


async def _existing(repo: StorageRepository, key: str) -> Notice:
    notice = await repo.get_notice(key)
    if notice is None:
        raise HTTPException(status_code=404, detail="Notice not found")
    return notice


@router.post("/{key:path}/dismiss", response_model=Notice)
async def dismiss_notice(key: str, repo: StorageRepository = Depends(get_repository)) -> Notice:
    """Dismiss for good. For an unregistered folder this is the "skip" answer."""
    notice = await _existing(repo, key)
    if notice.catalog_id == "unregistered_dir":
        await dismiss_dir(key.split(":", 1)[1])
    return await repo.set_notice_status(key, NoticeStatus.DISMISSED, utc_now())  # type: ignore[return-value]


@router.post("/{key:path}/snooze", response_model=Notice)
async def snooze_notice(
    key: str,
    hours: float = Query(gt=0, le=24 * 365),
    repo: StorageRepository = Depends(get_repository),
) -> Notice:
    """Hide until ``hours`` from now; it becomes active again afterwards."""
    await _existing(repo, key)
    now = utc_now()
    return await repo.set_notice_status(  # type: ignore[return-value]
        key, NoticeStatus.SNOOZED, now, snoozed_until=now + timedelta(hours=hours),
    )


@router.post("/{key:path}/clear", response_model=Notice)
async def clear_notice(key: str, repo: StorageRepository = Depends(get_repository)) -> Notice:
    """Mark resolved; a detector hit later revives it."""
    await _existing(repo, key)
    return await repo.set_notice_status(key, NoticeStatus.CLEARED, utc_now())  # type: ignore[return-value]


@router.get("/{key:path}")
async def get_notice(
    key: str,
    host: Literal["cc", "codex"] = "cc",
    repo: StorageRepository = Depends(get_repository),
) -> dict[str, Any]:
    """Full detail: parameters, model notes in both languages, every delivery."""
    notice = await _existing(repo, key)
    entry = CATALOG.get(notice.catalog_id)
    host = notice.host or host
    rendered = {}
    if entry is not None:
        for language in ("zh", "en"):
            result = render_entry(entry, variant=notice.variant, language=language, host=host, params=notice.params)
            rendered[language] = {"user_line": result.plain, "model_note": result.model}
    deliveries = await repo.list_notice_deliveries(keys=[key])
    return {
        "notice": notice.model_dump(mode="json"),
        "design_number": DESIGN_NUMBER.get(notice.catalog_id, ""),
        "kind": entry.kind.value if entry else "",
        "color": entry.color.value if entry else "",
        "severity": entry.severity.value if entry else "",
        "rendered": rendered,
        "deliveries": [delivery.model_dump(mode="json") for delivery in deliveries],
    }
