"""Explicit external exec registration and minimum usage receipts, no execution API."""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Body, Depends, HTTPException, Query

from aiteam.api.deps import get_repository
from aiteam.services.external_exec_reconciliation import reconcile_external_exec
from aiteam.storage.connection import DEFAULT_DB_URL
from aiteam.storage.external_exec import ExternalExecConflictError, ExternalExecRepository
from aiteam.storage.repository import StorageRepository
from aiteam.types import (
    ExternalExecFinish,
    ExternalExecQuarantineRegistration,
    ExternalExecReceipt,
    ExternalExecRegistration,
)

router = APIRouter(prefix="/api/external-execs", tags=["external-execs"])


def get_external_exec_repository(
    repository: StorageRepository = Depends(get_repository),
) -> ExternalExecRepository:
    return ExternalExecRepository(repository._db_url or DEFAULT_DB_URL)


async def _association(repository: StorageRepository, registration: ExternalExecRegistration) -> None:
    if registration.project_id is not None and await repository.get_project(registration.project_id) is None:
        raise HTTPException(404, detail={"code": "project_not_found", "message": "项目不存在"})
    if registration.task_id is not None:
        task = await repository.get_task(registration.task_id)
        if task is None:
            raise HTTPException(404, detail={"code": "task_not_found", "message": "任务不存在"})
        if task.project_id != registration.project_id:
            raise HTTPException(409, detail={"code": "task_project_mismatch", "message": "任务与执行必须属于同一项目"})


def _error(error: Exception):
    if isinstance(error, KeyError):
        raise HTTPException(404, detail="外部执行未登记") from error
    if isinstance(error, ExternalExecConflictError):
        raise HTTPException(409, detail=str(error)) from error
    raise HTTPException(422, detail="外部执行账据不符合白名单约束") from error


async def _response(repository, invocation_id, fields: Literal["compact", "all"] = "compact") -> dict:
    item = await repository.get_page(invocation_id, receipt_limit=100 if fields == "all" else 0)
    if item is None:
        raise HTTPException(404, detail="外部执行未登记")
    return {
        "success": True,
        "data": item.model_dump(mode="json", exclude={"receipts"} if fields == "compact" else set()),
    }


@router.post("")
async def register(
    registration: ExternalExecRegistration,
    repository: ExternalExecRepository = Depends(get_external_exec_repository),
    project_repository: StorageRepository = Depends(get_repository),
    fields: Literal["compact", "all"] = "compact",
) -> dict:
    existing = await repository.get_page(registration.invocation_id, receipt_limit=0)
    if existing is not None:
        if not repository.same_registration(existing, registration):
            _error(ExternalExecConflictError("invocation_identity_conflict"))
        return await _response(repository, registration.invocation_id, fields)
    await _association(project_repository, registration)
    try:
        item = await repository.register(registration, accepted=True)
    except ValueError as error:
        _error(error)
    return await _response(repository, item.invocation_id, fields)


@router.post("/quarantine")
async def quarantine(
    body: ExternalExecQuarantineRegistration,
    repository: ExternalExecRepository = Depends(get_external_exec_repository),
    project_repository: StorageRepository = Depends(get_repository),
) -> dict:
    existing = await repository.get_page(body.registration.invocation_id, receipt_limit=0)
    if existing is not None and repository.same_registration(existing, body.registration):
        # An immutable successful registration can survive project deletion; it
        # cannot be converted to quarantine by a client-supplied reason.
        return await _response(repository, existing.invocation_id)
    try:
        await _association(project_repository, body.registration)
    except HTTPException as error:
        actual = error.detail.get("code") if isinstance(error.detail, dict) else None
        if actual != body.reason_code:
            raise HTTPException(
                409, detail={"code": "association_reason_mismatch", "message": "关联状态已变化"}
            ) from error
    else:
        raise HTTPException(409, detail={"code": "association_valid", "message": "有效关联不能隔离"})
    try:
        item = await repository.register_quarantined(body.registration, body.reason_code)
    except ValueError as error:
        _error(error)
    return await _response(repository, item.invocation_id)


@router.get("")
async def list_invocations(
    project_id: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    repository: ExternalExecRepository = Depends(get_external_exec_repository),
    fields: Literal["compact", "all"] = "compact",
) -> dict:
    items, total = await repository.list_compact(project_id=project_id, limit=limit, offset=offset)
    if fields == "all":
        items = [await repository.get_page(item.invocation_id, receipt_limit=100) for item in items]
    return {
        "success": True,
        "data": {
            "items": [
                item.model_dump(mode="json", exclude={"receipts"} if fields == "compact" else set()) for item in items
            ],
            "total": total,
            "limit": limit,
            "offset": offset,
            "truncated": offset + len(items) < total,
        },
    }


@router.get("/reconciliation")
async def reconciliation(
    project_id: str | None = None,
    source_namespace: str | None = Query(default=None, pattern=r"^[0-9a-f]{64}$"),
    since: str | None = None,
    until: str | None = None,
    limit: int = Query(default=500, ge=1, le=500),
    bucket_offset: int = Query(default=0, ge=0),
    bucket_limit: int = Query(default=20, ge=0, le=50),
    summary: bool = False,
    repository: ExternalExecRepository = Depends(get_external_exec_repository),
) -> dict:
    from aiteam.services.codex_usage_export import parse_usage_time

    try:
        data = await reconcile_external_exec(
            repository,
            project_id=project_id,
            source_namespace=source_namespace,
            since=parse_usage_time(since) if since is not None else None,
            until=parse_usage_time(until) if until is not None else None,
            limit=limit,
            bucket_offset=bucket_offset,
            bucket_limit=bucket_limit,
            summary=summary,
        )
    except ValueError as error:
        raise HTTPException(422, detail="时间窗口必须含时区且起点不晚于终点") from error
    return {"success": True, "data": data}


@router.get("/{invocation_id}")
async def get_invocation(
    invocation_id: str,
    repository: ExternalExecRepository = Depends(get_external_exec_repository),
    receipt_offset: int = Query(default=0, ge=0),
    receipt_limit: int = Query(default=100, ge=0, le=100),
) -> dict:
    item = await repository.get_page(invocation_id, receipt_offset=receipt_offset, receipt_limit=receipt_limit)
    if item is None:
        raise HTTPException(404, detail="外部执行未登记")
    return {"success": True, "data": item.model_dump(mode="json")}


@router.post("/{invocation_id}/thread")
async def bind_thread(
    invocation_id: str,
    thread_id: Annotated[str, Body(embed=True, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/+-]{0,255}$")],
    repository: ExternalExecRepository = Depends(get_external_exec_repository),
    fields: Literal["compact", "all"] = "compact",
) -> dict:
    try:
        item = await repository.bind_thread(invocation_id, thread_id)
    except (KeyError, ValueError) as error:
        _error(error)
    return await _response(repository, item.invocation_id, fields)


@router.post("/{invocation_id}/receipt")
async def receipt(
    invocation_id: str,
    receipt: ExternalExecReceipt,
    repository: ExternalExecRepository = Depends(get_external_exec_repository),
    fields: Literal["compact", "all"] = "compact",
) -> dict:
    try:
        item = await repository.receipt(invocation_id, receipt)
    except (KeyError, ValueError) as error:
        _error(error)
    return await _response(repository, item.invocation_id, fields)


@router.post("/{invocation_id}/finish")
async def finish(
    invocation_id: str,
    finish: ExternalExecFinish,
    repository: ExternalExecRepository = Depends(get_external_exec_repository),
    fields: Literal["compact", "all"] = "compact",
) -> dict:
    try:
        item = await repository.finish(invocation_id, finish)
    except (KeyError, ValueError) as error:
        _error(error)
    return await _response(repository, item.invocation_id, fields)
