"""Account-scoped, on-demand captures and explicitly attributed usage batches.

No login or retrospective account assignment is performed. Periodic native
quota reads use saved monitor settings and default on after a confirmed capture.
The existing project filter is deliberately not applied to account-wide limits.
"""

from __future__ import annotations

import asyncio
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Body, Depends, HTTPException, Path, Request

from aiteam.api.deps import get_repository
from aiteam.api.routes.pricing import _catalog, _validate_json_keys
from aiteam.clock import utc_now
from aiteam.services.account_monitor import AccountMonitorRunner
from aiteam.services.account_monitor import capture_monitor_account as capture_account
from aiteam.services.account_usage import estimate_batch
from aiteam.services.codex_account_capture import (
    CodexAccountCaptureError,
)
from aiteam.services.plan_capacity import estimate_plan_capacity
from aiteam.services.plan_pricing import estimate_pricing_plan_capacity
from aiteam.storage.account_monitor import MonitorRepository
from aiteam.storage.account_usage import AccountUsageRepository
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
) -> dict:
    accounts = await repository.list_accounts()
    return {"success": True, "data": {"accounts": [a.model_dump(mode="json") for a in accounts]}}


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
            "plan_estimates": [
                item.model_dump(mode="json") for item in estimate_plan_capacity(
                    await repository.list_plan_snapshots(account.account_key), now=utc_now(),
                )
            ],
            "pricing_plan_estimates": [
                item.model_dump(mode="json") for item in estimate_pricing_plan_capacity(
                    await repository.list_plan_price_snapshots(account.account_key), now=utc_now(),
                    anchors=await repository.list_plan_anchors(account.account_key),
                )
            ],
        },
    }


@router.get("/{account_key}")
async def get_account(
    account_key: AccountKey,
    repository: AccountUsageRepository = Depends(get_account_repository),
    include_pricing: bool = True,
) -> dict:
    account = await _require_account(repository, account_key)
    snapshots = await repository.list_snapshots(account_key)
    by_id = {snapshot.snapshot_id: snapshot for snapshot in snapshots}
    batches = await repository.list_batches(account_key) if include_pricing else []
    catalog = _catalog() if batches else None
    estimates = []
    for batch in batches:
        try:
            result = estimate_batch(
                batch, by_id[batch.start_snapshot_id], by_id[batch.end_snapshot_id], catalog,
            )
        except (KeyError, ValueError) as exc:
            raise HTTPException(409, detail="历史批次与采样记录不一致，未返回错误的费用估算") from exc
        estimates.append(result.model_dump(mode="json"))
    return {
        "success": True,
        "data": {
            "account": account.model_dump(mode="json"),
            "snapshots": [s.model_dump(mode="json") for s in snapshots],
            "estimates": estimates,
            "plan_estimates": [
                item.model_dump(mode="json") for item in estimate_plan_capacity(
                    await repository.list_plan_snapshots(account_key), now=utc_now(),
                )
            ],
            "pricing_plan_estimates": [
                item.model_dump(mode="json") for item in estimate_pricing_plan_capacity(
                    await repository.list_plan_price_snapshots(account_key), now=utc_now(),
                    anchors=await repository.list_plan_anchors(account_key),
                )
            ],
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
