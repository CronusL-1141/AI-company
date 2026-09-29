"""Account-scoped, on-demand captures and explicitly attributed usage batches.

No login or retrospective account assignment is performed. Periodic native
quota reads use saved monitor settings and default on after a confirmed capture.
The existing project filter is deliberately not applied to account-wide limits.
"""

from __future__ import annotations

import asyncio
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Body, Depends, HTTPException, Path, Query, Request

from aiteam.api.deps import get_repository
from aiteam.api.routes.pricing import _catalog, _validate_json_keys
from aiteam.clock import parse_utc, utc_now
from aiteam.services.account_monitor import AccountMonitorRunner
from aiteam.services.account_monitor import capture_monitor_account as capture_account
from aiteam.services.account_usage import estimate_batch
from aiteam.services.codex_account_capture import (
    CodexAccountCaptureError,
)
from aiteam.storage.account_monitor import MonitorRepository
from aiteam.storage.account_usage import AccountUsageRepository, SummaryUnavailableError
from aiteam.storage.connection import DEFAULT_DB_URL
from aiteam.storage.repository import StorageRepository
from aiteam.types import PricingAccount, PricingMonitorSettings, PricingPlanAnchorReset, PricingUsageBatch

router = APIRouter(prefix="/api/account-usage", tags=["account-usage"])
_capture_lock = asyncio.Lock()
AccountKey = Annotated[str, Path(pattern=r"^[0-9a-f]{64}$")]


def get_account_repository(
    repository: StorageRepository = Depends(get_repository),
) -> AccountUsageRepository:
    """Keep the selected database; never switch to a second implicit database."""
    return AccountUsageRepository(repository._db_url or DEFAULT_DB_URL)


def get_monitor_repository(
    repository: AccountUsageRepository = Depends(get_account_repository),
) -> MonitorRepository:
    return MonitorRepository(repository._db_url)


def get_monitor_runner(request: Request) -> AccountMonitorRunner | None:
    return getattr(request.app.state, "account_monitor_runner", None)


async def _require_account(repository: AccountUsageRepository, key: str) -> PricingAccount:
    account = await repository.get_account(key)
    if account is None:
        raise HTTPException(404, detail="账号尚未采样；请先采样当前 Codex 账号")
    return account


@router.get("")
async def list_accounts(
    repository: AccountUsageRepository = Depends(get_account_repository),
    runner: AccountMonitorRunner | None = Depends(get_monitor_runner),
) -> dict:
    accounts = await repository.list_accounts()
    current_key = runner.current_account_key if runner is not None else None
    return {"success": True, "data": {
        "accounts": [a.model_dump(mode="json") for a in accounts],
        "current_account_key": current_key if any(a.account_key == current_key for a in accounts) else None,
    }}


@router.post("/capture")
async def capture(
    repository: AccountUsageRepository = Depends(get_account_repository),
    monitors: MonitorRepository = Depends(get_monitor_repository),
) -> dict:
    """Capture the native current account and weekly limits, without a model turn."""
    if _capture_lock.locked():
        raise HTTPException(409, detail="已有一次账号采样正在进行，请等待它结束")
    async with _capture_lock:
        claim = await monitors.claim_source(f"manual-{uuid4()}", utc_now())
        if claim is None:
            raise HTTPException(409, detail="本机 Codex 登录源正在采样，请稍后再试")
        try:
            try:
                captured = await capture_account(repository=repository)
                account, snapshots = captured[:2]
                plans = captured[2] if len(captured) >= 3 else []
                prices = captured[3] if len(captured) >= 4 else []
            except CodexAccountCaptureError as exc:
                # Only curated errors, never native stderr or credentials.
                raise HTTPException(503, detail=str(exc)) from exc
            except SummaryUnavailableError as exc:
                raise HTTPException(503, detail="套餐统计摘要暂时无法更新，此次额度未保存，请稍后重试") from exc
            saved = await monitors.save_source_capture(
                claim, account, snapshots, plan_snapshots=plans, pricing_plan_snapshots=prices,
            )
            if saved is None:
                raise HTTPException(409, detail="采样占用已过期，旧结果未保存；请重新采样")
            account, snapshots = saved
        finally:
            await monitors.release_source(claim)
    return {
        "success": True,
        "data": {
            "account": account.model_dump(mode="json"),
            "snapshots": [s.model_dump(mode="json") for s in snapshots],
            **await _estimates(repository, account.account_key),
        },
    }


async def _estimates(repository: AccountUsageRepository, account_key: str) -> dict:
    """Both plan estimates from the incremental summaries: O(1) in the history length."""
    now = utc_now()
    try:
        plan = await repository.plan_estimates(account_key, now=now)
        pricing = await repository.pricing_plan_estimates(account_key, now=now)
    except SummaryUnavailableError as exc:
        raise HTTPException(503, detail="套餐统计摘要正在重建，请稍后刷新") from exc
    return {
        "plan_estimates": [item.model_dump(mode="json") for item in plan],
        "pricing_plan_estimates": [item.model_dump(mode="json") for item in pricing],
    }


@router.get("/{account_key}")
async def get_account(
    account_key: AccountKey,
    repository: AccountUsageRepository = Depends(get_account_repository),
    include_pricing: bool = True,
) -> dict:
    """Account summary: estimates, batch estimates and the latest capture.

    The full quota history is not returned here any more (it grew to ~18K rows at a
    30-second interval and was re-read on every page refresh): page through it with
    ``GET /{account_key}/snapshots``.
    """
    account = await _require_account(repository, account_key)
    batches = await repository.list_batches(account_key) if include_pricing else []
    catalog = _catalog() if batches else None
    estimates = []
    for batch in batches:
        start = await repository.get_snapshot(batch.start_snapshot_id)
        end = await repository.get_snapshot(batch.end_snapshot_id)
        try:
            if start is None or end is None or start.account_key != account_key or end.account_key != account_key:
                raise KeyError(batch.batch_id)
            result = estimate_batch(batch, start, end, catalog)
        except (KeyError, ValueError) as exc:
            raise HTTPException(409, detail="历史批次与采样记录不一致，未返回错误的费用估算") from exc
        estimates.append(result.model_dump(mode="json"))
    latest, _ = await repository.page_snapshots(account_key, limit=16)
    latest_time = latest[0].observed_at if latest else None
    return {
        "success": True,
        "data": {
            "account": account.model_dump(mode="json"),
            "snapshot_count": await repository.count_snapshots(account_key),
            "latest_snapshots": [
                s.model_dump(mode="json") for s in latest if s.observed_at == latest_time
            ],
            "estimates": estimates,
            **await _estimates(repository, account_key),
        },
    }


@router.get("/{account_key}/snapshots")
async def list_account_snapshots(
    account_key: AccountKey,
    repository: AccountUsageRepository = Depends(get_account_repository),
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    before: Annotated[str | None, Query(max_length=300)] = None,
) -> dict:
    """Quota observations newest first; pass ``next_cursor`` back as ``before``."""
    await _require_account(repository, account_key)
    cursor = None
    if before is not None:
        observed_at, _, snapshot_id = before.partition("|")
        parsed = parse_utc(observed_at) if snapshot_id else None
        if parsed is None:
            raise HTTPException(422, detail="before 游标格式无效")
        cursor = (parsed, snapshot_id)
    items, next_cursor = await repository.page_snapshots(account_key, limit=limit, before=cursor)
    return {
        "success": True,
        "data": {
            "items": [item.model_dump(mode="json") for item in items],
            "next_cursor": None if next_cursor is None else f"{next_cursor[0].isoformat()}|{next_cursor[1]}",
            "total": await repository.count_snapshots(account_key),
        },
    }


@router.post("/{account_key}/plan-anchor/reset", dependencies=[Depends(_validate_json_keys)])
async def reset_plan_anchor(
    account_key: AccountKey,
    request: PricingPlanAnchorReset,
    repository: AccountUsageRepository = Depends(get_account_repository),
) -> dict:
    """Use the latest saved paired sample without invoking native capture."""
    try:
        anchor = await repository.reset_plan_anchor(account_key, request)
    except LookupError as exc:
        raise HTTPException(404, detail="账号尚未采样；请先采样当前 Codex 账号") from exc
    except SummaryUnavailableError as exc:
        raise HTTPException(503, detail="套餐统计摘要正在重建，请稍后重试") from exc
    except ValueError as exc:
        raise HTTPException(409, detail=str(exc)) from exc
    return {"success": True, "data": anchor.model_dump(mode="json")}


@router.patch("/{account_key}/label", dependencies=[Depends(_validate_json_keys)])
async def rename_account(
    account_key: AccountKey,
    label: Annotated[str, Body(embed=True, min_length=1, max_length=80)],
    repository: AccountUsageRepository = Depends(get_account_repository),
) -> dict:
    account = await _require_account(repository, account_key)
    if not label.strip():
        raise HTTPException(422, detail="账号别名不能为空")
    account = account.model_copy(update={"label": label.strip()})
    await repository.upsert_account(account)
    return {"success": True, "data": account.model_dump(mode="json")}


@router.post("/{account_key}/batches", dependencies=[Depends(_validate_json_keys)])
async def add_batch(
    account_key: AccountKey,
    batch: PricingUsageBatch,
    repository: AccountUsageRepository = Depends(get_account_repository),
) -> dict:
    """Store a caller-attributed batch; confirmation is a claim, never native evidence."""
    await _require_account(repository, account_key)
    if batch.account_key != account_key:
        raise HTTPException(422, detail="导入批次的账号与页面所选账号不一致")
    start = await repository.get_snapshot(batch.start_snapshot_id)
    end = await repository.get_snapshot(batch.end_snapshot_id)
    if start is None or end is None:
        raise HTTPException(422, detail="起止采样不存在；不能用当前时间补齐历史水位")
    try:
        result = estimate_batch(batch, start, end, _catalog())
        await repository.add_batch(batch)
    except ValueError as exc:
        # Values here are our own validation failures, not native process output.
        raise HTTPException(409, detail=str(exc)) from exc
    return {"success": True, "data": result.model_dump(mode="json")}


@router.get("/{account_key}/monitor")
async def get_monitor(
    account_key: AccountKey,
    repository: MonitorRepository = Depends(get_monitor_repository),
    runner: AccountMonitorRunner | None = Depends(get_monitor_runner),
) -> dict:
    """Read saved settings, including unchanged legacy periods up to 24 hours."""
    try:
        state = await repository.get(account_key)
    except ValueError as exc:
        raise HTTPException(404, detail="账号尚未绑定；请先连接本机 Codex 账号") from exc
    state = state.model_copy(update={"runtime_running": bool(runner and runner.is_running)})
    return {"success": True, "data": state.model_dump(mode="json")}


@router.put("/{account_key}/monitor", dependencies=[Depends(_validate_json_keys)])
async def configure_monitor(
    account_key: AccountKey,
    settings: PricingMonitorSettings,
    repository: MonitorRepository = Depends(get_monitor_repository),
    runner: AccountMonitorRunner | None = Depends(get_monitor_runner),
) -> dict:
    """Set a 30-second to 30-minute period; omit it to pause without replacing it."""
    if settings.enabled and (runner is None or not runner.is_running):
        raise HTTPException(503, detail="OS 账号监控执行器未启动；未启用监控")
    try:
        await repository.get(account_key)
    except ValueError as exc:
        raise HTTPException(404, detail="账号尚未绑定；请先连接本机 Codex 账号") from exc
    try:
        state = await repository.configure(account_key, settings)
    except ValueError as exc:
        raise HTTPException(409, detail=str(exc)) from exc
    if runner is not None:
        await runner.settings_changed(account_key)
    state = state.model_copy(update={"runtime_running": bool(runner and runner.is_running)})
    return {"success": True, "data": state.model_dump(mode="json")}
