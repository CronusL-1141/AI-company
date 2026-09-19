"""Read account quota snapshots through the native Codex app-server protocol.

The default binary belongs to the macOS desktop application. Other installations
require an explicit AITEAM_CODEX_BINARY path; terminal PATH discovery is not used.
Account information is compared only in memory. Returned account keys are SHA-256
digests of the account ID supplied by the quota endpoint, never of an email.

Protocol source: https://learn.chatgpt.com/docs/app-server#auth-endpoints
The native server owns authentication and may refresh managed credentials itself.
This client never reads credentials or requests login, refresh, switching or turns.
Authentication failures and server requests for refreshed credentials are errors.
The bracket checks observed endpoints, not ownership throughout an interval.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import ValidationError

from aiteam.clock import from_timestamp, utc_now
from aiteam.types import PlanUsageSnapshot, PricingAccount, PricingQuotaSnapshot

_DESKTOP_BINARY = Path("/Applications/ChatGPT.app/Contents/Resources/codex")
_CAPTURE_TIMEOUT_SECONDS = 17.0
_USAGE_TIMEOUT_SECONDS = 3.0
_STOP_TIMEOUT_SECONDS = 1.0
_MAX_RESPONSE_BYTES = 262_144
_WEEK_MINUTES = 10_080


class CodexAccountCaptureError(RuntimeError):
    """A safe capture error without upstream identity or credential details."""


class _RequestUnavailableError(CodexAccountCaptureError):
    """A completed non-auth request failure that optional activity may omit."""


def _resolve_binary() -> Path:
    configured = os.environ.get("AITEAM_CODEX_BINARY")
    if configured is not None:
        binary = Path(configured).expanduser()
        if not configured.strip() or not binary.is_absolute():
            raise CodexAccountCaptureError("AITEAM_CODEX_BINARY 必须是明确的可执行文件绝对路径。")
    elif sys.platform == "darwin":
        binary = _DESKTOP_BINARY
    else:
        raise CodexAccountCaptureError("当前平台未配置原生 Codex；请设置 AITEAM_CODEX_BINARY。")
    if not binary.is_file() or not os.access(binary, os.X_OK):
        raise CodexAccountCaptureError("原生 Codex 可执行文件不可用，请检查 AITEAM_CODEX_BINARY。")
    return binary


class _ReadOnlyClient:
    def __init__(self, process: asyncio.subprocess.Process) -> None:
        self.process = process
        self.next_id = 0
        self.abandoned_ids: set[int] = set()

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        await self._send({"method": method, "params": params})

    async def _send(self, message: dict[str, Any]) -> None:
        if self.process.stdin is None:
            raise CodexAccountCaptureError("Codex 采样通道不可用。")
        self.process.stdin.write(json.dumps(message).encode("utf-8") + b"\n")
        await self.process.stdin.drain()

    async def request(self, method: str, params: dict[str, Any]) -> tuple[dict[str, Any], datetime]:
        self.next_id += 1
        request_id = self.next_id
        await self._send({"id": request_id, "method": method, "params": params})
        if self.process.stdout is None:
            raise CodexAccountCaptureError("Codex 采样通道不可用。")
        while True:
            line = await self.process.stdout.readline()
            received_at = utc_now()
            if not line:
                raise CodexAccountCaptureError("Codex 在完成采样前关闭了通道。")
            if len(line) > _MAX_RESPONSE_BYTES:
                raise CodexAccountCaptureError("Codex 响应超过采样大小限制。")
            try:
                message = json.loads(line)
            except (ValueError, UnicodeError):
                raise CodexAccountCaptureError("Codex 返回了无效的协议响应。") from None
            if not isinstance(message, dict):
                raise CodexAccountCaptureError("Codex 返回了无效的协议响应。")
            if "method" in message:
                if "id" in message:
                    raise CodexAccountCaptureError("Codex 请求额外交互或授权刷新，当前只读采集不支持。")
                continue
            if "error" in message:
                error = message["error"]
                detail = str(error.get("message", "")).lower() if isinstance(error, dict) else ""
                auth_terms = ("auth", "refresh", "401", "login", "credential", "expired token", "invalid token")
                auth_code = isinstance(error, dict) and error.get("code") in (401, 403)
                if auth_code or any(term in detail for term in auth_terms):
                    raise CodexAccountCaptureError("账号授权读取失败或需要刷新，当前只读采集不支持。")
            response_id = message.get("id")
            if type(response_id) is int and response_id in self.abandoned_ids:
                self.abandoned_ids.remove(response_id)
                continue
            if response_id != request_id:
                raise CodexAccountCaptureError("Codex 采样响应与请求不匹配。")
            if "error" in message:
                raise _RequestUnavailableError("Codex 账号或额度接口读取失败，当前采样不可用。")
            result = message.get("result")
            if not isinstance(result, dict):
                raise _RequestUnavailableError("Codex 返回了无效的采样结果。")
            return result, received_at


def _account_details(result: dict[str, Any]) -> dict[str, Any]:
    account = result.get("account")
    if not isinstance(account, dict) or account.get("type") != "chatgpt":
        raise CodexAccountCaptureError("当前原生 Codex 未提供已登录的 ChatGPT 账号。")
    if "email" not in account or not isinstance(account.get("planType"), str):
        raise CodexAccountCaptureError("Codex 账号信息不完整，无法核对采样归属。")
    if account["email"] is not None and not isinstance(account["email"], str):
        raise CodexAccountCaptureError("Codex 账号信息格式无效。")
    return account


def _quota_account_id(result: dict[str, Any]) -> str:
    account_id = result.get("accountId")
    if not isinstance(account_id, str) or not account_id.strip():
        raise CodexAccountCaptureError("额度响应缺少账号归属，无法保存此次采样。")
    return account_id


def _quota_buckets(result: dict[str, Any]) -> dict[str, Any]:
    if "rateLimitsByLimitId" in result:
        buckets = result["rateLimitsByLimitId"]
        if buckets is None:
            buckets = {}
        if not isinstance(buckets, dict):
            raise CodexAccountCaptureError("Codex 多桶额度数据格式无效。")
    else:
        legacy = result.get("rateLimits")
        if not isinstance(legacy, dict):
            raise CodexAccountCaptureError("Codex 未返回可用的额度数据。")
        limit_id = legacy.get("limitId")
        if not isinstance(limit_id, str) or not limit_id.strip():
            raise CodexAccountCaptureError("Codex 旧版额度响应缺少桶标识。")
        buckets = {limit_id: legacy}
    return buckets


def _weekly_snapshots(
    result: dict[str, Any], account_key: str, observed_at: datetime,
) -> list[PricingQuotaSnapshot]:
    snapshots: list[PricingQuotaSnapshot] = []
    for limit_id, bucket in _quota_buckets(result).items():
        if not isinstance(limit_id, str) or not limit_id.strip() or not isinstance(bucket, dict):
            raise CodexAccountCaptureError("Codex 额度桶格式无效。")
        if bucket.get("limitId") not in (None, limit_id):
            raise CodexAccountCaptureError("Codex 额度桶标识不一致。")
        for window_name in ("primary", "secondary"):
            window = bucket.get(window_name)
            if window is None:
                continue
            if not isinstance(window, dict):
                raise CodexAccountCaptureError("Codex 额度窗口格式无效。")
            minutes = window.get("windowDurationMins")
            if type(minutes) is not int or minutes != _WEEK_MINUTES:
                continue
            used = window.get("usedPercent")
            reset = window.get("resetsAt")
            if type(used) is not int or type(reset) is not int:
                raise CodexAccountCaptureError("Codex 周额度缺少有效的用量或重置时间。")
            try:
                snapshots.append(PricingQuotaSnapshot(
                    snapshot_id=str(uuid4()), account_key=account_key, limit_id=limit_id,
                    used_percent=Decimal(used), window_duration_ms=minutes * 60_000,
                    resets_at=from_timestamp(reset), observed_at=observed_at,
                    source="codex_app_server",
                ))
            except (ValueError, OverflowError, OSError, ValidationError):
                raise CodexAccountCaptureError("Codex 周额度数据超出有效范围。") from None
    if not snapshots:
        raise CodexAccountCaptureError("Codex 未返回可用的每周额度窗口，本次采样未保存。")
    return snapshots


async def _optional_account_usage(client: _ReadOnlyClient) -> tuple[dict[str, Any] | None, datetime | None]:
    request_id = client.next_id + 1
    try:
        async with asyncio.timeout(_USAGE_TIMEOUT_SECONDS):
            return await client.request("account/usage/read", {})
    except _RequestUnavailableError:
        return None, None
    except TimeoutError:
        # A late response must not be mistaken for the following quota response.
        client.abandoned_ids.add(request_id)
        return None, None


def _activity_fields(
    usage: dict[str, Any] | None, activity_observed_at: datetime | None, observed_at: datetime,
) -> dict[str, Any]:
    # This is a receipt bracket, not a freshness guarantee. The native profile
    # summary can lag quota reads by hours and exposes no accounting cutoff.
    # Retain the measurement for diagnosis; the estimator must not treat it as
    # an activity counter aligned with the current allowance observation.
    unavailable = {"activity_tokens": None, "activity_scope": None, "activity_observed_at": None}
    if usage is None or activity_observed_at is None:
        return unavailable
    if not 0 <= (observed_at - activity_observed_at).total_seconds() <= 60:
        return unavailable
    summary = usage.get("summary")
    tokens = summary.get("lifetimeTokens") if isinstance(summary, dict) else None
    buckets = usage.get("dailyUsageBuckets")
    if (
        type(tokens) is not int or not 0 <= tokens <= 9_007_199_254_740_991
        or not isinstance(buckets, list) or not buckets
    ):
        return unavailable
    dates: set[str] = set()
    for bucket in buckets:
        if not isinstance(bucket, dict):
            return unavailable
        start_date = bucket.get("startDate")
        daily_tokens = bucket.get("tokens")
        if (
            not isinstance(start_date, str) or not start_date.strip()
            or type(daily_tokens) is not int or daily_tokens < 0
        ):
            return unavailable
        dates.add(start_date)
    scope = hashlib.sha256(json.dumps(sorted(dates), separators=(",", ":")).encode("utf-8")).hexdigest()
    return {"activity_tokens": tokens, "activity_scope": scope, "activity_observed_at": activity_observed_at}


def _plan_snapshots(
    result: dict[str, Any], account_key: str, observed_at: datetime,
    usage: dict[str, Any] | None, activity_observed_at: datetime | None,
) -> tuple[list[PricingQuotaSnapshot], list[PlanUsageSnapshot]]:
    activity = _activity_fields(usage, activity_observed_at, observed_at)
    quotas: list[PricingQuotaSnapshot] = []
    plans: list[PlanUsageSnapshot] = []
    for limit_id, bucket in _quota_buckets(result).items():
        if not isinstance(limit_id, str) or not limit_id.strip() or not isinstance(bucket, dict):
            raise CodexAccountCaptureError("Codex 额度桶格式无效。")
        if bucket.get("limitId") not in (None, limit_id):
            raise CodexAccountCaptureError("Codex 额度桶标识不一致。")
        for window_name in ("primary", "secondary"):
            window = bucket.get(window_name)
            if not isinstance(window, dict):
                continue
            minutes, used, reset = (
                window.get("windowDurationMins"), window.get("usedPercent"), window.get("resetsAt"),
            )
            if type(minutes) is not int or minutes <= 0 or type(used) is not int or type(reset) is not int:
                continue
            try:
                quota = PricingQuotaSnapshot(
                    snapshot_id=str(uuid4()), account_key=account_key, limit_id=limit_id,
                    used_percent=Decimal(used), window_duration_ms=minutes * 60_000,
                    resets_at=from_timestamp(reset), observed_at=observed_at, source="codex_app_server",
                )
                plan = PlanUsageSnapshot(
                    snapshot_id=quota.snapshot_id, account_key=account_key, limit_id=limit_id,
                    used_percent=used, window_duration_ms=quota.window_duration_ms,
                    resets_at=quota.resets_at, observed_at=observed_at, **activity,
                )
            except (ValueError, OverflowError, OSError, ValidationError):
                continue
            quotas.append(quota)
            plans.append(plan)
    if not plans:
        raise CodexAccountCaptureError("Codex 未返回可用的额度窗口，本次采样未保存。")
    return quotas, plans


async def _discard_stdout(stream: asyncio.StreamReader) -> None:
    while await stream.read(65_536):
        pass


async def _stop_process(process: asyncio.subprocess.Process) -> None:
    # A paused stdout pipe can prevent Process.wait() from completing after exit.
    discard = asyncio.create_task(_discard_stdout(process.stdout)) if process.stdout is not None else None
    if process.stdin is not None:
        process.stdin.close()
    try:
        if process.returncode is None:
            try:
                process.terminate()
            except ProcessLookupError:
                pass
        try:
            await asyncio.wait_for(process.wait(), timeout=_STOP_TIMEOUT_SECONDS)
        except TimeoutError:
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
            try:
                await asyncio.wait_for(process.wait(), timeout=_STOP_TIMEOUT_SECONDS)
            except TimeoutError:
                raise CodexAccountCaptureError("Codex 诊断子进程未能在限定时间内退出。") from None
    finally:
        if discard is not None:
            discard.cancel()
            await asyncio.gather(discard, return_exceptions=True)


async def _finish_process_cleanup(
    process: asyncio.subprocess.Process, cancellation: asyncio.CancelledError | None,
) -> None:
    # Only this capture's child is owned here. Repeated caller cancellation must
    # not interrupt its bounded terminate/kill/wait sequence or leave it running.
    cleanup = asyncio.create_task(_stop_process(process))
    while not cleanup.done():
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError as error:
            if cancellation is None:
                cancellation = error
        except Exception:
            break
    try:
        cleanup.result()
    except (Exception, asyncio.CancelledError) as error:
        if cancellation is not None:
            raise cancellation from error
        raise
    if cancellation is not None:
        raise cancellation


async def _capture_account(
    include_activity: bool,
    *, all_windows: bool = False,
) -> tuple[PricingAccount, list[PricingQuotaSnapshot], list[PlanUsageSnapshot]]:
    """Capture a stable account bracket with optional activity, without a model turn.

    Seventeen seconds cover startup and all reads. Shutdown has two bounded
    one-second waits and always reaps the child, including after cancellation.
    Stderr is discarded by the OS so it cannot leak details or block the child.
    """
    binary = _resolve_binary()
    process: asyncio.subprocess.Process | None = None
    cancellation: asyncio.CancelledError | None = None
    try:
        async with asyncio.timeout(_CAPTURE_TIMEOUT_SECONDS):
            process = await asyncio.create_subprocess_exec(
                str(binary), "app-server", "--stdio", "-c", "analytics.enabled=false",
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL, limit=_MAX_RESPONSE_BYTES,
            )
            client = _ReadOnlyClient(process)
            await client.request("initialize", {
                "clientInfo": {"name": "aiteam_account_quota", "version": "1.0.0"},
            })
            await client.notify("initialized", {})
            before, _ = await client.request("account/read", {"refreshToken": False})
            initial_account = _account_details(before)
            first_limits, _ = await client.request("account/rateLimits/read", {"excludeResetCreditDetails": True})
            first_account_id = _quota_account_id(first_limits)
            usage, activity_observed_at = await _optional_account_usage(client) if include_activity else (None, None)
            final_limits, observed_at = await client.request(
                "account/rateLimits/read", {"excludeResetCreditDetails": True},
            )
            final_account_id = _quota_account_id(final_limits)
            after, _ = await client.request("account/read", {"refreshToken": False})
            if first_account_id != final_account_id or initial_account != _account_details(after):
                raise CodexAccountCaptureError("采样期间账号发生变化，此次额度未保存，请重试。")
            account_key = hashlib.sha256(final_account_id.encode("utf-8")).hexdigest()
            if include_activity or all_windows:
                snapshots, plans = _plan_snapshots(final_limits, account_key, observed_at, usage, activity_observed_at)
            else:
                snapshots, plans = _weekly_snapshots(final_limits, account_key, observed_at), []
            account = PricingAccount(
                account_key=account_key, label=f"Codex {account_key[-8:]}", created_at=observed_at,
            )
            return account, snapshots, plans
    except asyncio.CancelledError as error:
        cancellation = error
        raise
    except TimeoutError:
        raise CodexAccountCaptureError("Codex 账号采样超时，此次额度未保存。") from None
    except (OSError, ValueError, UnicodeError):
        raise CodexAccountCaptureError("Codex 账号采样通道失败或返回了无效数据。") from None
    finally:
        if process is not None:
            await _finish_process_cleanup(process, cancellation)


async def capture_account() -> tuple[PricingAccount, list[PricingQuotaSnapshot]]:
    """Preserve the existing weekly-only account capture contract."""
    account, snapshots, _ = await _capture_account(include_activity=False)
    return account, snapshots


async def capture_plan_account() -> tuple[PricingAccount, list[PricingQuotaSnapshot], list[PlanUsageSnapshot]]:
    """Capture all quota windows and optional native account activity for capacity estimates."""
    return await _capture_account(include_activity=True)


async def capture_plan_quota() -> tuple[PricingAccount, list[PricingQuotaSnapshot], list[PlanUsageSnapshot]]:
    """Read all quota windows without the delayed remote activity profile."""
    return await _capture_account(include_activity=False, all_windows=True)
