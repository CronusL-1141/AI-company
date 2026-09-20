"""API-lifetime quota sampling with capture-confirmed default monitoring."""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from aiteam.clock import utc_now
from aiteam.services.codex_account_capture import CodexAccountCaptureError
from aiteam.services.local_plan_capture import _sample_source, _source_fence, capture_local_plan_account
from aiteam.storage.account_monitor import MonitorRepository
from aiteam.storage.account_usage import AccountUsageRepository
from aiteam.types import PlanUsageSnapshot, PricingAccount, PricingPlanSnapshot, PricingQuotaSnapshot

_CaptureResult = (
    tuple[PricingAccount, list[PricingQuotaSnapshot]]
    | tuple[PricingAccount, list[PricingQuotaSnapshot], list[PlanUsageSnapshot]]
    | tuple[PricingAccount, list[PricingQuotaSnapshot], list[PlanUsageSnapshot], list[PricingPlanSnapshot]]
)

_POLL_SECONDS = 10.0
_RENEW_SECONDS = 10.0
_CAPTURE_DEADLINE_SECONDS = 30.0  # native read/reap (19s) + bounded local scan (8s)
_ROUND_DEADLINE_SECONDS = 35.0
_RELEASE_DEADLINE_SECONDS = 3.0
_LEASE_MS = 60_000
_BOOTSTRAP_ATTEMPTS = 3
_BOOTSTRAP_RETRY_SECONDS = 30.0
_BOOTSTRAP_SLOW_RETRY_SECONDS = 300.0
_ROUND_TIMEOUT_ERROR = "监控采样轮次超时，本轮未完成；已安排下次重试。"
_logger = logging.getLogger(__name__)


def _login_source_stamp() -> tuple:
    """Cheap login-change hint; never read authentication or configuration contents."""
    root = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))).expanduser().resolve()
    return root, _source_fence(root)


async def capture_monitor_account(*, repository: AccountUsageRepository) -> _CaptureResult:
    """Require stable local source metadata as well as the native account fence."""
    try:
        before = await asyncio.to_thread(_sample_source)
    except (OSError, ValueError) as error:
        raise CodexAccountCaptureError("本机账号来源暂不可确认，此次额度未保存，请重试。") from error
    result = await capture_local_plan_account(repository=repository)
    try:
        after = await asyncio.to_thread(_sample_source)
    except (OSError, ValueError) as error:
        raise CodexAccountCaptureError("采样期间账号来源发生变化，此次额度未保存，请重试。") from error
    if before != after:
        raise CodexAccountCaptureError("采样期间账号来源发生变化，此次额度未保存，请重试。")
    return result


class AccountMonitorRunner:
    """Coordinate due sampling with persistent source leases and fencing.

    No native process is started until the repository grants a source or due claim.
    The injected collector uses the same typed contract as capture_account;
    storage revalidates all returned observations before committing them.
    """

    def __init__(
        self,
        repository: MonitorRepository,
        *,
        capture: Callable[[], Awaitable[_CaptureResult]] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._repository = repository
        self._capture = capture or self._capture_local
        self._clock = clock or utc_now
        self._owner = str(uuid4())
        self._task: asyncio.Task[None] | None = None
        self._capture_task: asyncio.Task[_CaptureResult] | None = None
        self._claim: dict[str, Any] | None = None
        self._wake = asyncio.Event()
        self._lifecycle_lock = asyncio.Lock()
        self._tick_lock = asyncio.Lock()
        self._stopping = False
        self._bootstrap_remaining = _BOOTSTRAP_ATTEMPTS
        self._bootstrap_next_at: datetime | None = None
        self._source_stamp: tuple | None = None
        self._current_account_key: str | None = None

    async def _capture_local(self) -> _CaptureResult:
        return await capture_monitor_account(
            repository=AccountUsageRepository(self._repository._db_url),
        )

    @property
    def current_account_key(self) -> str | None:
        """Last successfully saved native identity, cleared when the source changes."""
        return self._current_account_key

    @property
    def is_running(self) -> bool:
        """Report this process's live runner task, not a saved enabled setting."""
        return self._task is not None and not self._task.done() and not self._stopping

    async def start(self) -> None:
        """Start once and inspect persisted due state without replaying history."""
        async with self._lifecycle_lock:
            if self._task is not None and not self._task.done():
                return
            await self._repository.init_db()
            self._stopping = False
            self._bootstrap_remaining = _BOOTSTRAP_ATTEMPTS
            self._bootstrap_next_at = None
            self._source_stamp = None
            self._current_account_key = None
            self._task = asyncio.create_task(self._run(), name="account-monitor")

    async def stop(self) -> None:
        """Cancel this runner and wait until its owned collector has been reaped."""
        async with self._lifecycle_lock:
            self._stopping = True
            self._wake.set()
            if self._task is not None:
                self._task.cancel()
            if self._capture_task is not None:
                self._capture_task.cancel()
            if self._task is not None:
                await self._drain_cancelled(self._task)
                self._task = None
            if self._capture_task is not None:
                await self._drain_cancelled(self._capture_task)

    async def settings_changed(self, account_key: str) -> None:
        """Wake for a configuration change and cancel that account's collector."""
        self._wake.set()
        claim = self._claim
        capture = self._capture_task
        state = claim.get("state") if claim is not None else None
        if claim is not None and capture is not None and (state is None or state.account_key == account_key):
            capture.cancel()
            await self._drain_cancelled(capture)

    @staticmethod
    async def _drain_cancelled(task: asyncio.Task[Any]) -> None:
        # Repeated outer cancellation must not detach native-process cleanup.
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        try:
            task.result()
        except (Exception, asyncio.CancelledError):
            pass

    async def _run(self) -> None:
        while not self._stopping:
            self._wake.clear()
            try:
                if not await self.bootstrap():
                    await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                # Repository failures still wait before retrying and never
                # expose upstream exception text or start an unclaimed sample.
                _logger.warning("Account monitor loop failed (%s)", type(error).__name__)
            if self._stopping:
                break
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=_POLL_SECONDS)
            except TimeoutError:
                pass

    async def bootstrap(self) -> bool:
        """Discover login changes; retry failures slowly after the initial burst."""
        async with self._tick_lock:
            try:
                stamp = await asyncio.to_thread(_login_source_stamp)
            except (OSError, ValueError):
                stamp = ()  # unavailable metadata is not an authenticated identity
            if stamp != self._source_stamp:
                self._source_stamp = stamp
                self._current_account_key = None
                self._bootstrap_remaining = _BOOTSTRAP_ATTEMPTS
                self._bootstrap_next_at = None
            if (not self._bootstrap_remaining and self._bootstrap_next_at is None) or (
                self._bootstrap_next_at is not None and self._clock() < self._bootstrap_next_at
            ):
                return False
            claim = None
            try:
                async with asyncio.timeout(_ROUND_DEADLINE_SECONDS):
                    claim = await self._repository.claim_source(self._owner, self._clock(), lease_ms=_LEASE_MS)
                    if claim is None:
                        return False
                    self._bootstrap_remaining = max(0, self._bootstrap_remaining - 1)
                    retry_seconds = (
                        _BOOTSTRAP_RETRY_SECONDS if self._bootstrap_remaining
                        else _BOOTSTRAP_SLOW_RETRY_SECONDS
                    )
                    self._bootstrap_next_at = self._clock() + timedelta(seconds=retry_seconds)
                    self._claim = claim
                    result = await self._collect_while_leased(claim)
                    if result is None:
                        return True
                    account, snapshots = result[:2]
                    plans = result[2] if len(result) >= 3 else []
                    prices = result[3] if len(result) >= 4 else None
                    saved = await self._repository.save_source_capture(
                        claim, account, snapshots, plan_snapshots=plans, pricing_plan_snapshots=prices,
                    )
                    if saved is not None:
                        self._bootstrap_remaining = 0
                        self._bootstrap_next_at = None
                        self._current_account_key = account.account_key
            except asyncio.CancelledError:
                raise
            except Exception as error:
                _logger.warning("Account monitor bootstrap failed (%s)", type(error).__name__)
            finally:
                capture = self._capture_task
                if capture is not None and not capture.done():
                    capture.cancel()
                    await self._drain_cancelled(capture)
                self._capture_task = None
                self._claim = None
                if claim is not None:
                    try:
                        async with asyncio.timeout(_RELEASE_DEADLINE_SECONDS):
                            await self._repository.release_source(claim)
                    except (Exception, asyncio.CancelledError) as error:
                        _logger.warning("Account monitor bootstrap release failed (%s)", type(error).__name__)
            return claim is not None

    async def tick(self) -> bool:
        """Attempt at most one due claim; return whether one was acquired."""
        async with self._tick_lock:
            claim: dict[str, Any] | None = None
            round_timed_out = False
            try:
                async with asyncio.timeout(_ROUND_DEADLINE_SECONDS):
                    claim = await self._repository.claim_due(self._owner, self._clock(), lease_ms=_LEASE_MS)
                    if claim is None:
                        return False
                    self._claim = claim
                    await self._sample_claim(claim)
            except TimeoutError:
                # The native collector has a shorter deadline and normally
                # records its timeout. This limit also bounds repository stalls.
                round_timed_out = True
                _logger.warning("Account monitor round exceeded its deadline (TimeoutError)")
            finally:
                capture = self._capture_task
                if capture is not None and not capture.done():
                    capture.cancel()
                    await self._drain_cancelled(capture)
                self._capture_task = None
                self._claim = None
                if claim is not None:
                    try:
                        async with asyncio.timeout(_RELEASE_DEADLINE_SECONDS):
                            if round_timed_out:
                                # finish checks the live fence in its transaction.
                                # Do not revisit a stalled renewal, or turn this
                                # failure into a normal waiting state via release.
                                recorded = await self._repository.finish(
                                    claim, None, [], error=_ROUND_TIMEOUT_ERROR,
                                )
                                if not recorded:
                                    _logger.warning(
                                        "Account monitor timeout was not recorded: claim no longer current (LeaseLost)",
                                    )
                            else:
                                await self._repository.release_claim(claim)
                    except (Exception, asyncio.CancelledError) as error:
                        # The bounded recovery shares the release budget. If it
                        # fails, leave the lease to expire and log only its type.
                        _logger.warning(
                            "Account monitor finalization failed (%s); lease expiry is the fallback",
                            type(error).__name__,
                        )
            return claim is not None

    async def _collect_while_leased(
        self, claim: dict[str, Any],
    ) -> _CaptureResult | None:
        capture = asyncio.create_task(self._capture(), name="account-monitor-capture")
        self._capture_task = capture
        renew = self._repository.renew_source if "source_key" in claim else self._repository.renew_claim
        try:
            async with asyncio.timeout(_CAPTURE_DEADLINE_SECONDS):
                while not capture.done():
                    done, _ = await asyncio.wait({capture}, timeout=_RENEW_SECONDS)
                    if not done and not await renew(claim, self._clock(), lease_ms=_LEASE_MS):
                        return None
                try:
                    return capture.result()
                except asyncio.CancelledError:
                    # settings_changed cancels only the collector; the runner
                    # stays alive to process the user's next enabled setting.
                    current = asyncio.current_task()
                    if current is not None and current.cancelling():
                        raise
                    return None
        finally:
            if not capture.done():
                capture.cancel()
                await self._drain_cancelled(capture)

    @staticmethod
    def _safe_failure(error: Exception) -> tuple[str, bool]:
        if isinstance(error, CodexAccountCaptureError):
            message = str(error)
            if any(term in message for term in ("授权", "已登录", "登录", "刷新", "额外交互")):
                return "账号授权不可用，监控已暂停；完成登录或授权后将自动恢复。", True
            if "账号发生变化" in message:
                return "采样期间账号发生变化，监控已暂停；确认当前登录账号后将自动恢复。", True
            if "超时" in message:
                return "原生账号采样超时，已安排下次重试。", False
        if isinstance(error, TimeoutError):
            return "原生账号采样超时，已安排下次重试。", False
        return "原生账号采样失败，已安排下次重试。", False

    async def _sample_claim(self, claim: dict[str, Any]) -> None:
        try:
            result = await self._collect_while_leased(claim)
        except Exception as error:
            message, pause = self._safe_failure(error)
            if await self._repository.renew_claim(claim, self._clock(), lease_ms=_LEASE_MS):
                if await self._repository.finish(claim, None, [], error=message, pause=pause) and pause:
                    self._current_account_key = None
                    self._bootstrap_remaining = _BOOTSTRAP_ATTEMPTS
                    self._bootstrap_next_at = self._clock() + timedelta(seconds=_BOOTSTRAP_RETRY_SECONDS)
            return
        if result is None:
            return
        if not await self._repository.renew_claim(claim, self._clock(), lease_ms=_LEASE_MS):
            return
        account, snapshots = result[:2]
        plans = result[2] if len(result) >= 3 else []
        prices = result[3] if len(result) == 4 else None
        expected_key = claim["state"].account_key
        if account.account_key != expected_key or any(snapshot.account_key != expected_key for snapshot in snapshots):
            await self._repository.finish(
                claim, None, [], error="当前原生账号与绑定账号不一致，监控已暂停；切回此账号后将自动恢复。", pause=True,
            )
            self._current_account_key = None
            self._bootstrap_remaining = _BOOTSTRAP_ATTEMPTS
            self._bootstrap_next_at = None
        elif not snapshots:
            await self._repository.finish(claim, None, [], error="此次未取得可用的周额度数据，已安排下次重试。")
        else:
            if await self._repository.finish(
                claim, account, snapshots, plan_snapshots=plans, pricing_plan_snapshots=prices,
            ):
                self._current_account_key = account.account_key
