"""Explicit-database storage for immutable account quota and request evidence."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager

from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession

from aiteam.clock import utc_now
from aiteam.services.account_usage import validate_batch_window
from aiteam.storage.connection import get_session
from aiteam.storage.engine_pool import engine_pool
from aiteam.storage.models import (
    AccountPlanSnapshotModel,
    AccountUsageAccountModel,
    AccountUsageBatchModel,
    AccountUsageRequestModel,
    AccountUsageSnapshotModel,
    Base,
)
from aiteam.types import PlanUsageSnapshot, PricingAccount, PricingQuotaSnapshot, PricingUsageBatch


class AccountUsageRepository:
    """Store account evidence in the explicitly selected SQLite database.

    SQLite's write reservation precedes all validation reads. Independent
    repository instances and processes therefore share the same arbitration,
    including atomic request ownership across differently named batches.
    """

    def __init__(self, db_url: str) -> None:
        if not isinstance(db_url, str) or not db_url.strip():
            raise ValueError("an explicit account usage database URL is required")
        if make_url(db_url).get_backend_name() != "sqlite":
            raise ValueError("account usage storage requires an explicit SQLite database")
        self._db_url = db_url

    async def init_db(self) -> None:
        """Create only this repository's tables, without shared migrations."""
        tables = [
            AccountUsageAccountModel.__table__,
            AccountUsageSnapshotModel.__table__,
            AccountUsageBatchModel.__table__,
            AccountUsageRequestModel.__table__,
            AccountPlanSnapshotModel.__table__,
        ]
        async with engine_pool.get_engine(self._db_url).begin() as connection:
            await connection.execute(text("BEGIN IMMEDIATE"))
            await connection.run_sync(Base.metadata.create_all, tables=tables)

    @asynccontextmanager
    async def _write_session(self) -> AsyncIterator[AsyncSession]:
        async with get_session(self._db_url) as session:
            await session.execute(text("BEGIN IMMEDIATE"))
            yield session

    @staticmethod
    async def _upsert_account(
        session: AsyncSession, account: PricingAccount,
    ) -> PricingAccount:
        row = await session.get(AccountUsageAccountModel, account.account_key)
        if row is None:
            session.add(AccountUsageAccountModel(
                account_key=account.account_key,
                created_at=account.created_at,
                payload=account.model_dump(mode="json"),
            ))
        else:
            stored = PricingAccount.model_validate(row.payload)
            account = account.model_copy(update={"created_at": stored.created_at})
            row.payload = account.model_dump(mode="json")
        await session.flush()
        return account

    async def upsert_account(self, account: PricingAccount) -> PricingAccount:
        """Update the alias while retaining the original account creation time."""
        account = PricingAccount.model_validate(account.model_dump(mode="python"))
        async with self._write_session() as session:
            return await self._upsert_account(session, account)

    async def get_account(self, account_key: str) -> PricingAccount | None:
        async with get_session(self._db_url) as session:
            row = await session.get(AccountUsageAccountModel, account_key)
            return None if row is None else PricingAccount.model_validate(row.payload)

    async def list_accounts(self) -> list[PricingAccount]:
        async with get_session(self._db_url) as session:
            rows = (await session.scalars(
                select(AccountUsageAccountModel).order_by(
                    AccountUsageAccountModel.created_at,
                    AccountUsageAccountModel.account_key,
                ),
            )).all()
            return [PricingAccount.model_validate(row.payload) for row in rows]

    @staticmethod
    async def _add_snapshot(
        session: AsyncSession, snapshot: PricingQuotaSnapshot,
    ) -> PricingQuotaSnapshot:
        row = await session.get(AccountUsageSnapshotModel, snapshot.snapshot_id)
        if row is not None:
            stored = PricingQuotaSnapshot.model_validate(row.payload)
            if stored != snapshot:
                raise ValueError("snapshot_id already exists with different content")
            return stored
        if await session.get(AccountUsageAccountModel, snapshot.account_key) is None:
            raise ValueError("snapshot account does not exist")
        session.add(AccountUsageSnapshotModel(
            snapshot_id=snapshot.snapshot_id,
            account_key=snapshot.account_key,
            observed_at=snapshot.observed_at,
            payload=snapshot.model_dump(mode="json"),
        ))
        await session.flush()
        return snapshot

    async def add_snapshot(self, snapshot: PricingQuotaSnapshot) -> PricingQuotaSnapshot:
        """Persist an observation once; reject changes to an existing ID."""
        snapshot = PricingQuotaSnapshot.model_validate(snapshot.model_dump(mode="python"))
        async with self._write_session() as session:
            return await self._add_snapshot(session, snapshot)

    async def save_capture(
        self, account: PricingAccount, snapshots: Sequence[PricingQuotaSnapshot],
        *, plan_snapshots: Sequence[PlanUsageSnapshot] = (),
    ) -> tuple[PricingAccount, list[PricingQuotaSnapshot]]:
        """Commit observations atomically without replacing an existing alias."""
        account = PricingAccount.model_validate(account.model_dump(mode="python"))
        validated = [
            PricingQuotaSnapshot.model_validate(snapshot.model_dump(mode="python"))
            for snapshot in snapshots
        ]
        if not validated:
            raise ValueError("a capture must contain at least one snapshot")
        if any(snapshot.account_key != account.account_key for snapshot in validated):
            raise ValueError("captured snapshots must belong to the captured account")
        plans = self._validate_plan_snapshots(account.account_key, validated, plan_snapshots)
        async with self._write_session() as session:
            row = await session.get(AccountUsageAccountModel, account.account_key)
            stored_account = (
                await self._upsert_account(session, account)
                if row is None else PricingAccount.model_validate(row.payload)
            )
            stored_snapshots = [
                await self._add_snapshot(session, snapshot) for snapshot in validated
            ]
            for plan in plans:
                await self._add_plan_snapshot(session, plan)
            return stored_account, stored_snapshots

    @staticmethod
    def _validate_plan_snapshots(
        account_key: str, snapshots: Sequence[PricingQuotaSnapshot],
        plan_snapshots: Sequence[PlanUsageSnapshot],
    ) -> list[PlanUsageSnapshot]:
        """Keep optional plan records inside their actual capture batch."""
        plans = [
            PlanUsageSnapshot.model_validate(snapshot.model_dump(mode="python"))
            for snapshot in plan_snapshots
        ]
        snapshot_ids = {snapshot.snapshot_id for snapshot in snapshots}
        if any(plan.account_key != account_key for plan in plans):
            raise ValueError("plan snapshots must belong to the captured account")
        if any(plan.snapshot_id not in snapshot_ids for plan in plans):
            raise ValueError("plan snapshots must match quota snapshots in the capture")
        return plans

    @staticmethod
    async def _add_plan_snapshot(
        session: AsyncSession, snapshot: PlanUsageSnapshot,
    ) -> PlanUsageSnapshot:
        """Store one immutable plan observation without altering existing quota data."""
        snapshot = PlanUsageSnapshot.model_validate(snapshot.model_dump(mode="python"))
        row = await session.get(AccountPlanSnapshotModel, snapshot.snapshot_id)
        if row is not None:
            stored = PlanUsageSnapshot.model_validate(row.payload)
            if stored != snapshot:
                raise ValueError("plan snapshot_id already exists with different content")
            return stored
        if await session.get(AccountUsageAccountModel, snapshot.account_key) is None:
            raise ValueError("plan snapshot account does not exist")
        quota = await session.get(AccountUsageSnapshotModel, snapshot.snapshot_id)
        if quota is None:
            raise ValueError("plan snapshot requires its corresponding quota snapshot")
        stored_quota = PricingQuotaSnapshot.model_validate(quota.payload)
        if any(getattr(snapshot, name) != getattr(stored_quota, name) for name in (
            "account_key", "limit_id", "window_duration_ms", "resets_at", "observed_at", "used_percent",
        )):
            raise ValueError("plan snapshot must match the quota account, window and observation")
        session.add(AccountPlanSnapshotModel(
            id=snapshot.snapshot_id, account_key=snapshot.account_key,
            observed_at=snapshot.observed_at, payload=snapshot.model_dump(mode="json"),
        ))
        await session.flush()
        return snapshot

    async def add_plan_snapshot(self, snapshot: PlanUsageSnapshot) -> PlanUsageSnapshot:
        """Add an observation for an already-persisted matching quota snapshot."""
        async with self._write_session() as session:
            return await self._add_plan_snapshot(session, snapshot)

    async def list_plan_snapshots(self, account_key: str) -> list[PlanUsageSnapshot]:
        """Read immutable plan observations in stable chronological order."""
        async with get_session(self._db_url) as session:
            rows = (await session.scalars(
                select(AccountPlanSnapshotModel)
                .where(AccountPlanSnapshotModel.account_key == account_key)
                .order_by(AccountPlanSnapshotModel.observed_at, AccountPlanSnapshotModel.id),
            )).all()
            return [PlanUsageSnapshot.model_validate(row.payload) for row in rows]

    async def get_snapshot(self, snapshot_id: str) -> PricingQuotaSnapshot | None:
        async with get_session(self._db_url) as session:
            row = await session.get(AccountUsageSnapshotModel, snapshot_id)
            return None if row is None else PricingQuotaSnapshot.model_validate(row.payload)

    async def list_snapshots(self, account_key: str) -> list[PricingQuotaSnapshot]:
        async with get_session(self._db_url) as session:
            rows = (await session.scalars(
                select(AccountUsageSnapshotModel)
                .where(AccountUsageSnapshotModel.account_key == account_key)
                .order_by(
                    AccountUsageSnapshotModel.observed_at,
                    AccountUsageSnapshotModel.snapshot_id,
                ),
            )).all()
            return [PricingQuotaSnapshot.model_validate(row.payload) for row in rows]

    async def add_batch(self, batch: PricingUsageBatch) -> PricingUsageBatch:
        """Validate a snapshot interval and claim every request in one commit."""
        batch = PricingUsageBatch.model_validate(batch.model_dump(mode="python"))
        async with self._write_session() as session:
            row = await session.get(AccountUsageBatchModel, batch.batch_id)
            if row is not None:
                stored = PricingUsageBatch.model_validate(row.payload)
                if stored != batch:
                    raise ValueError("batch_id already exists with different content")
                return stored
            start = await session.get(AccountUsageSnapshotModel, batch.start_snapshot_id)
            end = await session.get(AccountUsageSnapshotModel, batch.end_snapshot_id)
            if start is None or end is None:
                raise ValueError("both batch snapshots must exist")
            validate_batch_window(
                batch,
                PricingQuotaSnapshot.model_validate(start.payload),
                PricingQuotaSnapshot.model_validate(end.payload),
            )
            request_ids = [entry.request.request_id for entry in batch.entries]
            if len(request_ids) != len(set(request_ids)):
                raise ValueError("request_id must be unique within the batch")
            if request_ids:
                existing = await session.scalar(
                    select(AccountUsageRequestModel.request_id)
                    .where(
                        AccountUsageRequestModel.request_id.in_(request_ids),
                    )
                    .limit(1),
                )
                if existing is not None:
                    raise ValueError("a request_id already belongs to another account batch")
            session.add(AccountUsageBatchModel(
                batch_id=batch.batch_id,
                account_key=batch.account_key,
                created_at=utc_now(),
                payload=batch.model_dump(mode="json"),
            ))
            session.add_all([
                AccountUsageRequestModel(
                    account_key=batch.account_key,
                    request_id=request_id,
                    batch_id=batch.batch_id,
                )
                for request_id in request_ids
            ])
            await session.flush()
            return batch

    async def get_batch(self, batch_id: str) -> PricingUsageBatch | None:
        async with get_session(self._db_url) as session:
            row = await session.get(AccountUsageBatchModel, batch_id)
            return None if row is None else PricingUsageBatch.model_validate(row.payload)

    async def list_batches(self, account_key: str) -> list[PricingUsageBatch]:
        async with get_session(self._db_url) as session:
            rows = (await session.scalars(
                select(AccountUsageBatchModel)
                .where(AccountUsageBatchModel.account_key == account_key)
                .order_by(AccountUsageBatchModel.created_at, AccountUsageBatchModel.batch_id),
            )).all()
            return [PricingUsageBatch.model_validate(row.payload) for row in rows]
