"""Persistent account sampling settings and fenced native-source arbitration."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession

from aiteam.clock import ensure_utc, utc_now
from aiteam.storage.account_usage import AccountUsageRepository
from aiteam.storage.connection import get_session
from aiteam.storage.engine_pool import engine_pool
from aiteam.storage.models import AccountUsageAccountModel, AccountUsageMonitorModel, Base
from aiteam.types import (
    PlanUsageSnapshot,
    PricingAccount,
    PricingMonitorSettings,
    PricingMonitorState,
    PricingPlanSnapshot,
    PricingQuotaSnapshot,
)

_SOURCE_KEY = "__native_source__"


def _decode_state(payload: dict[str, Any]) -> PricingMonitorState:
    """Retain legacy saved intervals without expanding accepted new settings."""
    return PricingMonitorState.model_validate(payload, context={"stored_monitor_settings": True})


class MonitorRepository:
    """Keep scheduling and capture commits inside the selected SQLite database."""

    def __init__(self, db_url: str) -> None:
        if not isinstance(db_url, str) or not db_url.strip():
            raise ValueError("an explicit monitor database URL is required")
        if make_url(db_url).get_backend_name() != "sqlite":
            raise ValueError("account monitoring requires an explicit SQLite database")
        self._db_url = db_url

    async def init_db(self) -> None:
        """Create only the monitor table without touching other schema."""
        async with engine_pool.get_engine(self._db_url).begin() as connection:
            await connection.execute(text("BEGIN IMMEDIATE"))
            await connection.run_sync(
                Base.metadata.create_all, tables=[AccountUsageMonitorModel.__table__],
            )

    @asynccontextmanager
    async def _write_session(self) -> AsyncIterator[AsyncSession]:
        async with get_session(self._db_url) as session:
            await session.execute(text("BEGIN IMMEDIATE"))
            yield session

    @staticmethod
    async def _require_account(session: AsyncSession, account_key: str) -> None:
        if await session.get(AccountUsageAccountModel, account_key) is None:
            raise ValueError("monitor account does not exist")

    @staticmethod
    def _store(row: AccountUsageMonitorModel, state: PricingMonitorState) -> None:
        row.payload = state.model_dump(mode="json")
        row.enabled = state.settings.enabled
        row.next_run_at = state.next_run_at

    async def get(self, account_key: str) -> PricingMonitorState:
        """Read without creating a monitor or scheduling background work."""
        async with get_session(self._db_url) as session:
            await self._require_account(session, account_key)
            row = await session.get(AccountUsageMonitorModel, account_key)
            return (
                PricingMonitorState(account_key=account_key)
                if row is None else _decode_state(row.payload)
            )

    async def configure(
        self, account_key: str, settings: PricingMonitorSettings,
    ) -> PricingMonitorState:
        """Invalidate old results while retaining any in-flight source lease."""
        interval_provided = "interval_ms" in settings.model_fields_set
        settings = PricingMonitorSettings.model_validate(settings.model_dump(mode="python"))
        async with self._write_session() as session:
            await self._require_account(session, account_key)
            if settings.enabled:
                other = await session.scalar(select(AccountUsageMonitorModel.account_key).where(
                    AccountUsageMonitorModel.enabled.is_(True),
                    AccountUsageMonitorModel.account_key != account_key,
                ).limit(1))
                if other is not None:
                    raise ValueError("another account monitor is enabled for this native source")
            row = await session.get(AccountUsageMonitorModel, account_key)
            previous = (
                PricingMonitorState(account_key=account_key)
                if row is None else _decode_state(row.payload)
            )
            if not interval_provided:
                saved_interval = previous.settings.interval_ms
                if settings.enabled and saved_interval > 1800000:
                    raise ValueError("旧监控周期超过30分钟；请明确选择30秒至30分钟的新周期后启用")
                settings = settings.model_copy(update={"interval_ms": saved_interval})
            state = previous.model_copy(update={
                "settings": settings, "revision": previous.revision + 1,
                "status": "waiting" if settings.enabled else "disabled",
                "next_run_at": utc_now() if settings.enabled else None,
                "last_error": None,
            })
            if row is None:
                row = AccountUsageMonitorModel(account_key=account_key, fence=0)
                session.add(row)
            self._store(row, state)
            await session.flush()
            return state

    @staticmethod
    def _lease_deadline(now: datetime, lease_ms: int) -> datetime:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("monitor lease times must be timezone-aware")
        if type(lease_ms) is not int or lease_ms <= 0:
            raise ValueError("lease_ms must be a positive integer")
        return ensure_utc(now) + timedelta(milliseconds=lease_ms)

    async def claim_source(
        self, owner: str, now: datetime, lease_ms: int = 60000,
    ) -> dict[str, Any] | None:
        """Reserve the same source for a manual capture without an account yet."""
        if not isinstance(owner, str) or not owner.strip() or len(owner) > 200:
            raise ValueError("monitor lease owner must be a nonempty identifier")
        deadline = self._lease_deadline(now, lease_ms)
        now = ensure_utc(now)
        async with self._write_session() as session:
            leased = await session.scalar(select(AccountUsageMonitorModel.account_key).where(
                AccountUsageMonitorModel.lease_until > now,
            ).limit(1))
            if leased is not None:
                return None
            row = await session.get(AccountUsageMonitorModel, _SOURCE_KEY)
            if row is None:
                row = AccountUsageMonitorModel(
                    account_key=_SOURCE_KEY, payload={}, enabled=False, fence=0,
                )
                session.add(row)
            row.fence += 1
            row.lease_owner = owner
            row.lease_until = deadline
            await session.flush()
            return {
                "source_key": _SOURCE_KEY, "owner": owner,
                "fence": row.fence, "lease_until": deadline,
            }

    async def release_source(self, claim: dict[str, Any]) -> bool:
        """Release a manual source reservation without affecting newer owners."""
        if claim.get("source_key") != _SOURCE_KEY:
            return False
        async with self._write_session() as session:
            row = await session.get(AccountUsageMonitorModel, _SOURCE_KEY)
            if not self._owns(row, claim):
                return False
            row.lease_owner = None
            row.lease_until = None
            await session.flush()
            return True

    async def renew_source(
        self, claim: dict[str, Any], now: datetime, lease_ms: int = 60000,
    ) -> bool:
        """Extend a live bootstrap lease without reviving an expired owner."""
        if claim.get("source_key") != _SOURCE_KEY:
            return False
        deadline = self._lease_deadline(now, lease_ms)
        now = ensure_utc(now)
        async with self._write_session() as session:
            row = await session.get(AccountUsageMonitorModel, _SOURCE_KEY)
            if not self._owns(row, claim) or row.lease_until is None or row.lease_until <= now:
                return False
            row.lease_until = deadline
            await session.flush()
            claim["lease_until"] = deadline
            return True

    async def _default_on_captured_account(
        self, session: AsyncSession, account_key: str, completed_at: datetime,
    ) -> None:
        """Bind defaults only after capture validation, preserving saved choices."""
        other_rows = (await session.scalars(select(AccountUsageMonitorModel).where(
            AccountUsageMonitorModel.enabled.is_(True),
            AccountUsageMonitorModel.account_key != account_key,
        ))).all()
        for other in other_rows:
            previous = _decode_state(other.payload)
            self._store(other, previous.model_copy(update={
                "settings": previous.settings.model_copy(update={"enabled": False}),
                "revision": previous.revision + 1,
                "status": "paused_account_changed", "next_run_at": None,
                "last_error": "当前原生账号与绑定账号不一致，监控已暂停；切回此账号后将自动恢复。",
            }))
        row = await session.get(AccountUsageMonitorModel, account_key)
        if row is not None:
            previous = _decode_state(row.payload)
            # A user disable has status "disabled" and always wins, including
            # configuration changes made while the native read was in flight.
            if previous.status == "paused_account_changed":
                self._store(row, previous.model_copy(update={
                    "settings": previous.settings.model_copy(update={"enabled": True}),
                    "revision": previous.revision + 1,
                    "status": "waiting", "last_error": None,
                    "last_finished_at": completed_at,
                    "next_run_at": completed_at + timedelta(milliseconds=previous.settings.interval_ms),
                }))
            elif previous.settings.enabled:
                # A manual capture or restart has already persisted fresh data;
                # surface its completion without postponing scheduled sampling.
                self._store(row, previous.model_copy(update={
                    "status": "waiting", "last_error": None,
                    "last_finished_at": completed_at,
                }))
            return
        settings = PricingMonitorSettings(enabled=True)
        state = PricingMonitorState(
            account_key=account_key, settings=settings, revision=1, status="waiting",
            last_finished_at=completed_at,
            next_run_at=completed_at + timedelta(milliseconds=settings.interval_ms),
        )
        row = AccountUsageMonitorModel(account_key=account_key, fence=0)
        self._store(row, state)
        session.add(row)

    async def save_source_capture(
        self, claim: dict[str, Any], account: PricingAccount,
        snapshots: Sequence[PricingQuotaSnapshot],
        *, plan_snapshots: Sequence[PlanUsageSnapshot] = (),
        pricing_plan_snapshots: Sequence[PricingPlanSnapshot] | None = None,
    ) -> tuple[PricingAccount, list[PricingQuotaSnapshot]] | None:
        """Commit a confirmed source capture and new defaults under one fence."""
        if claim.get("source_key") != _SOURCE_KEY:
            return None
        async with self._write_session() as session:
            row = await session.get(AccountUsageMonitorModel, _SOURCE_KEY)
            if not self._owns(row, claim) or row.lease_until is None or row.lease_until <= utc_now():
                return None
            account = PricingAccount.model_validate(account.model_dump(mode="python"))
            validated = [
                PricingQuotaSnapshot.model_validate(snapshot.model_dump(mode="python"))
                for snapshot in snapshots
            ]
            if not validated:
                raise ValueError("a capture must contain at least one snapshot")
            if any(snapshot.account_key != account.account_key for snapshot in validated):
                raise ValueError("captured snapshots must belong to the captured account")
            plans = AccountUsageRepository._validate_plan_snapshots(
                account.account_key, validated, plan_snapshots,
            )
            prices = AccountUsageRepository._validate_pricing_plan_snapshots(
                account.account_key, validated, pricing_plan_snapshots,
            )
            existing = await session.get(AccountUsageAccountModel, account.account_key)
            stored_account = (
                await AccountUsageRepository._upsert_account(session, account)
                if existing is None else PricingAccount.model_validate(existing.payload)
            )
            stored_snapshots = [
                await AccountUsageRepository._add_snapshot(session, snapshot)
                for snapshot in validated
            ]
            for plan in plans:
                await AccountUsageRepository._add_plan_snapshot(session, plan)
            for price in prices:
                await AccountUsageRepository._add_pricing_plan_snapshot(session, price)
            await self._default_on_captured_account(session, account.account_key, utc_now())
            if row.lease_until <= utc_now():
                await session.rollback()
                return None
            row.lease_owner = None
            row.lease_until = None
            await session.flush()
            return stored_account, stored_snapshots

    async def claim_due(
        self, owner: str, now: datetime, lease_ms: int = 60000,
    ) -> dict[str, Any] | None:
        """Claim one due account, excluding all unexpired native-source leases."""
        if not isinstance(owner, str) or not owner.strip() or len(owner) > 200:
            raise ValueError("monitor lease owner must be a nonempty identifier")
        deadline = self._lease_deadline(now, lease_ms)
        now = ensure_utc(now)
        async with self._write_session() as session:
            leased = await session.scalar(select(AccountUsageMonitorModel.account_key).where(
                AccountUsageMonitorModel.lease_until > now,
            ).limit(1))
            if leased is not None:
                return None
            row = await session.scalar(select(AccountUsageMonitorModel).where(
                AccountUsageMonitorModel.enabled.is_(True),
                AccountUsageMonitorModel.next_run_at <= now,
            ).order_by(
                AccountUsageMonitorModel.next_run_at, AccountUsageMonitorModel.account_key,
            ).limit(1))
            if row is None:
                return None
            state = _decode_state(row.payload).model_copy(update={
                "status": "sampling", "last_started_at": now,
                "next_run_at": deadline, "last_error": None,
            })
            row.fence += 1
            row.lease_owner = owner
            row.lease_until = deadline
            self._store(row, state)
            await session.flush()
            return {
                "state": state, "revision": state.revision, "fence": row.fence,
                "owner": owner, "lease_until": deadline,
            }

    @staticmethod
    def _owns(row: AccountUsageMonitorModel | None, claim: dict[str, Any]) -> bool:
        return (
            row is not None and row.lease_owner == claim["owner"]
            and row.fence == claim["fence"]
        )

    @classmethod
    def _current(
        cls, row: AccountUsageMonitorModel | None, claim: dict[str, Any], now: datetime,
    ) -> bool:
        return (
            cls._owns(row, claim) and row.enabled and row.lease_until is not None
            and row.lease_until > now
            and _decode_state(row.payload).revision == claim["revision"]
        )

    async def renew_claim(
        self, claim: dict[str, Any], now: datetime, lease_ms: int = 60000,
    ) -> bool:
        """Extend only a live lease whose settings revision remains current."""
        deadline = self._lease_deadline(now, lease_ms)
        now = ensure_utc(now)
        async with self._write_session() as session:
            row = await session.get(AccountUsageMonitorModel, claim["state"].account_key)
            if not self._current(row, claim, now):
                return False
            row.lease_until = deadline
            state = _decode_state(row.payload).model_copy(update={"next_run_at": deadline})
            self._store(row, state)
            await session.flush()
            claim["lease_until"] = deadline
            return True

    async def release_claim(self, claim: dict[str, Any]) -> bool:
        """Release cancelled work, including leases invalidated by configuration."""
        async with self._write_session() as session:
            row = await session.get(AccountUsageMonitorModel, claim["state"].account_key)
            if not self._owns(row, claim):
                return False
            row.lease_owner = None
            row.lease_until = None
            state = _decode_state(row.payload)
            if state.revision == claim["revision"] and state.status == "sampling":
                state = state.model_copy(update={
                    "status": "waiting" if state.settings.enabled else "disabled",
                    "next_run_at": (
                        utc_now() + timedelta(milliseconds=state.settings.interval_ms)
                        if state.settings.enabled else None
                    ),
                })
                self._store(row, state)
            await session.flush()
            return True

    async def finish(
        self, claim: dict[str, Any], account: PricingAccount | None,
        snapshots: Sequence[PricingQuotaSnapshot], error: str | None = None,
        pause: bool = False,
        *, plan_snapshots: Sequence[PlanUsageSnapshot] = (),
        pricing_plan_snapshots: Sequence[PricingPlanSnapshot] | None = None,
    ) -> bool:
        """Commit a current capture and its next due time in one transaction."""
        async with self._write_session() as session:
            row = await session.get(AccountUsageMonitorModel, claim["state"].account_key)
            if not self._current(row, claim, utc_now()):
                return False
            state = _decode_state(row.payload)
            if not error and not pause:
                if account is None or not snapshots:
                    raise ValueError("successful monitor capture requires an account and snapshots")
                account = PricingAccount.model_validate(account.model_dump(mode="python"))
                validated = [
                    PricingQuotaSnapshot.model_validate(snapshot.model_dump(mode="python"))
                    for snapshot in snapshots
                ]
                if account.account_key != state.account_key or any(
                    snapshot.account_key != state.account_key for snapshot in validated
                ):
                    raise ValueError("monitor capture account changed")
                plans = AccountUsageRepository._validate_plan_snapshots(
                    state.account_key, validated, plan_snapshots,
                )
                prices = AccountUsageRepository._validate_pricing_plan_snapshots(
                    state.account_key, validated, pricing_plan_snapshots,
                )
                for snapshot in validated:
                    await AccountUsageRepository._add_snapshot(session, snapshot)
                for plan in plans:
                    await AccountUsageRepository._add_plan_snapshot(session, plan)
                for price in prices:
                    await AccountUsageRepository._add_pricing_plan_snapshot(session, price)
            completed_at = utc_now()
            if not self._current(row, claim, completed_at):
                await session.rollback()
                return False
            state = state.model_copy(update={
                "status": "paused_account_changed" if pause else "error" if error else "waiting",
                "last_finished_at": completed_at,
                "next_run_at": (
                    None if pause else completed_at + timedelta(milliseconds=state.settings.interval_ms)
                ),
                "last_error": error[:500] if error else None,
            })
            self._store(row, state)
            row.lease_owner = None
            row.lease_until = None
            await session.flush()
            return True
