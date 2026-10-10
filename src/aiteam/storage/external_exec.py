"""Durable minimum external-exec facts; receipts and lifecycle commit atomically."""

from __future__ import annotations

import json

from sqlalchemy import func, literal_column, select, text, true
from sqlalchemy.engine import make_url

from aiteam.storage.connection import get_session
from aiteam.storage.engine_pool import engine_pool
from aiteam.storage.models import Base, ExternalExecInvocationModel
from aiteam.types import (
    ExternalExecFinish,
    ExternalExecInvocation,
    ExternalExecInvocationCompact,
    ExternalExecReceipt,
    ExternalExecRegistration,
)


class ExternalExecConflictError(ValueError):
    """A retry attempted to change an immutable invocation or receipt fact."""


class ExternalExecRepository:
    """One table works in the API DB and an independent local audit spool."""

    def __init__(self, db_url: str) -> None:
        if not db_url or make_url(db_url).get_backend_name() != "sqlite":
            raise ValueError("external exec requires an explicit SQLite database")
        self._db_url = db_url

    async def init_db(self) -> None:
        async with engine_pool.get_engine(self._db_url).begin() as connection:
            await connection.execute(text("BEGIN IMMEDIATE"))
            await connection.run_sync(
                Base.metadata.create_all,
                tables=[ExternalExecInvocationModel.__table__],
            )

    @staticmethod
    def _value(row: ExternalExecInvocationModel) -> ExternalExecInvocation:
        return ExternalExecInvocation.model_validate(row.payload)

    @staticmethod
    def _save(row: ExternalExecInvocationModel, value: ExternalExecInvocation) -> None:
        value = ExternalExecInvocation.model_validate(value.model_dump(mode="python"))
        row.project_id = value.project_id
        row.thread_id = value.thread_id
        row.started_at = value.started_at
        row.revision = value.revision
        row.payload = value.model_dump(mode="json")

    @staticmethod
    def same_registration(existing: ExternalExecInvocation, registration: ExternalExecRegistration) -> bool:
        return {
            key: getattr(existing, key) for key in ExternalExecRegistration.model_fields
        } == registration.model_dump()

    async def register(
        self,
        registration: ExternalExecRegistration,
        *,
        accepted: bool = False,
    ) -> ExternalExecInvocation:
        registration = ExternalExecRegistration.model_validate(registration.model_dump(mode="python"))
        async with get_session(self._db_url) as session:
            await session.execute(text("BEGIN IMMEDIATE"))
            row = await session.get(ExternalExecInvocationModel, registration.invocation_id)
            if row is not None:
                existing = self._value(row)
                if not self.same_registration(existing, registration):
                    raise ExternalExecConflictError("invocation_identity_conflict")
                return existing
            value = ExternalExecInvocation(
                **registration.model_dump(), association_status="accepted" if accepted else "unverified"
            )
            row = ExternalExecInvocationModel(invocation_id=value.invocation_id, uploaded_revision=0)
            self._save(row, value)
            session.add(row)
            return value

    async def register_quarantined(
        self,
        registration: ExternalExecRegistration,
        reason: str,
    ) -> ExternalExecInvocation:
        """Caller must validate failed association first; never replace a valid row."""
        canonical = registration.model_copy(update={"project_id": None, "task_id": None})
        async with get_session(self._db_url) as session:
            await session.execute(text("BEGIN IMMEDIATE"))
            row = await session.get(ExternalExecInvocationModel, registration.invocation_id)
            if row is not None:
                existing = self._value(row)
                if self.same_registration(existing, registration):
                    return existing  # Concurrent legitimate registration wins.
                if (
                    existing.association_status == "quarantined"
                    and self.same_registration(existing, canonical)
                    and existing.requested_project_id == registration.project_id
                    and existing.requested_task_id == registration.task_id
                    and existing.association_reason == reason
                ):
                    return existing
                raise ExternalExecConflictError("invocation_identity_conflict")
            value = ExternalExecInvocation(
                **canonical.model_dump(),
                association_status="quarantined",
                association_reason=reason,
                requested_project_id=registration.project_id,
                requested_task_id=registration.task_id,
                diagnostics=["association_quarantined"],
            )
            row = ExternalExecInvocationModel(invocation_id=value.invocation_id, uploaded_revision=0)
            self._save(row, value)
            session.add(row)
            return value

    async def association(self, invocation_id: str, status: str, reason: str | None = None) -> ExternalExecInvocation:
        """Only delivery metadata may change after sealing execution facts."""
        async with get_session(self._db_url) as session:
            await session.execute(text("BEGIN IMMEDIATE"))
            row = await session.get(ExternalExecInvocationModel, invocation_id)
            if row is None:
                raise KeyError(invocation_id)
            value = self._value(row)
            if value.association_status == status and value.association_reason == reason:
                return value
            if value.association_status in ("quarantined", "rejected"):
                raise ExternalExecConflictError("association_already_resolved")
            if status == "quarantined":
                value.requested_project_id, value.requested_task_id = value.project_id, value.task_id
                value.project_id = value.task_id = None
                value.diagnostics = list(dict.fromkeys([*value.diagnostics, "association_quarantined"]))[:20]
            elif status == "rejected":
                value.diagnostics = list(dict.fromkeys([*value.diagnostics, "association_rejected"]))[:20]
            value.association_status, value.association_reason = status, reason
            value.revision += 1
            self._save(row, value)
            return value

    async def diagnostic(self, invocation_id: str, code: str) -> None:
        async with get_session(self._db_url) as session:
            await session.execute(text("BEGIN IMMEDIATE"))
            row = await session.get(ExternalExecInvocationModel, invocation_id)
            if row is None:
                raise KeyError(invocation_id)
            value = self._value(row)
            if code not in value.diagnostics:
                value.diagnostics = [*value.diagnostics, code][:20]
                value.revision += 1
                self._save(row, value)

    async def get(self, invocation_id: str) -> ExternalExecInvocation | None:
        async with get_session(self._db_url) as session:
            row = await session.get(ExternalExecInvocationModel, invocation_id)
            return self._value(row) if row is not None else None

    @staticmethod
    def _bind(value: ExternalExecInvocation, thread_id: str | None) -> bool:
        if thread_id is None:
            return False
        # Reuse the shared whitelist at this persistence boundary.
        candidate = value.model_copy(update={"thread_id": thread_id})
        ExternalExecInvocation.model_validate(candidate.model_dump())
        if value.thread_id is not None and value.thread_id != thread_id:
            raise ExternalExecConflictError("invocation_thread_conflict")
        if value.thread_id == thread_id:
            return False
        value.thread_id = thread_id
        return True

    async def bind_thread(self, invocation_id: str, thread_id: str) -> ExternalExecInvocation:
        async with get_session(self._db_url) as session:
            await session.execute(text("BEGIN IMMEDIATE"))
            row = await session.get(ExternalExecInvocationModel, invocation_id)
            if row is None:
                raise KeyError(invocation_id)
            value = self._value(row)
            if value.status != "running" and value.thread_id != thread_id:
                raise ExternalExecConflictError("invocation_sealed")
            if self._bind(value, thread_id):
                value.revision += 1
                self._save(row, value)
            return value

    async def receipt(self, invocation_id: str, receipt: ExternalExecReceipt) -> ExternalExecInvocation:
        receipt = ExternalExecReceipt.model_validate(receipt.model_dump(mode="python"))
        async with get_session(self._db_url) as session:
            await session.execute(text("BEGIN IMMEDIATE"))
            row = await session.get(ExternalExecInvocationModel, invocation_id)
            if row is None:
                raise KeyError(invocation_id)
            value = self._value(row)
            previous = value.receipts.get(receipt.receipt_id)
            if value.status != "running":
                if previous is not None and previous.model_dump(exclude={"observed_at"}) == receipt.model_dump(
                    exclude={"observed_at"}
                ):
                    return value
                raise ExternalExecConflictError("invocation_sealed")
            if previous is not None:
                # Missing fields may arrive later; known facts cannot be revised.
                merged = previous.model_dump(mode="python")
                incoming = receipt.model_dump(mode="python")
                for key, new in incoming.items():
                    if key in ("observed_at", "usage", "metadata_source") or new is None:
                        continue
                    if merged[key] is not None and merged[key] != new:
                        raise ExternalExecConflictError("usage_receipt_conflict")
                    merged[key] = new
                if incoming["usage"] is not None:
                    tokens = merged["usage"] or {}
                    for key, new in incoming["usage"].items():
                        if new is None:
                            continue
                        if tokens.get(key) is not None and tokens[key] != new:
                            raise ExternalExecConflictError("usage_receipt_conflict")
                        tokens[key] = new
                    merged["usage"] = tokens
                if receipt.metadata_source == "explicit_observation":
                    merged["metadata_source"] = receipt.metadata_source
                receipt = ExternalExecReceipt.model_validate(merged)
                if receipt == previous:
                    return value
            self._bind(value, receipt.thread_id)
            # A native turn ID cannot be counted twice under different receipt keys.
            if receipt.turn_id is not None and any(
                other.receipt_id != receipt.receipt_id and other.turn_id == receipt.turn_id
                for other in value.receipts.values()
            ):
                raise ExternalExecConflictError("duplicate_native_turn")
            if previous is None and len(value.receipts) >= 10000:
                raise ExternalExecConflictError("receipt_limit")
            value.receipts[receipt.receipt_id] = receipt
            if receipt.usage is not None and any(number is not None for number in receipt.usage.model_dump().values()):
                value.diagnostics = [code for code in value.diagnostics if code != "missing_usage"]
            value.revision += 1
            self._save(row, value)
            return value

    async def finish(self, invocation_id: str, finish: ExternalExecFinish) -> ExternalExecInvocation:
        finish = ExternalExecFinish.model_validate(finish.model_dump(mode="python"))
        if finish.status == "completed" and finish.exit_code not in (None, 0):
            raise ValueError("completed_requires_success_exit")
        async with get_session(self._db_url) as session:
            await session.execute(text("BEGIN IMMEDIATE"))
            row = await session.get(ExternalExecInvocationModel, invocation_id)
            if row is None:
                raise KeyError(invocation_id)
            value = self._value(row)
            if finish.receipt_ids is not None and set(finish.receipt_ids) != set(value.receipts):
                raise ExternalExecConflictError("incomplete_receipt_set")
            if finish.finished_at < value.started_at:
                raise ValueError("finish_before_start")
            if value.status != "running" and finish.thread_id is not None and value.thread_id != finish.thread_id:
                raise ExternalExecConflictError("invocation_sealed")
            bound = self._bind(value, finish.thread_id)
            if value.status != "running":
                if value.status != finish.status or value.exit_code != finish.exit_code:
                    raise ExternalExecConflictError("invocation_finish_conflict")
                if bound:
                    value.revision += 1
                    self._save(row, value)
                return value
            value.status = finish.status
            value.finished_at = finish.finished_at
            value.exit_code = finish.exit_code
            value.diagnostics = list(dict.fromkeys([*value.diagnostics, *finish.diagnostics]))[:20]
            value.revision += 1
            self._save(row, value)
            return value

    async def list(
        self,
        *,
        project_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
        pending_only: bool = False,
        source_namespace: str | None = None,
        since=None,
        until=None,
    ) -> tuple[list[ExternalExecInvocation], int]:
        predicates = []
        model = ExternalExecInvocationModel
        if project_id is not None:
            predicates.append(model.project_id == project_id)
        if source_namespace is not None:
            # Namespace stays in the minimal payload and is selected in SQLite.
            predicates.append(model.payload["source_namespace"].as_string() == source_namespace)
        if pending_only:
            predicates.append(model.uploaded_revision < model.revision)
            predicates.append(model.payload["association_status"].as_string().is_distinct_from("rejected"))
        if since is not None:
            predicates.append(model.started_at >= since)
        if until is not None:
            predicates.append(model.started_at <= until)
        async with get_session(self._db_url) as session:
            total = int(await session.scalar(select(func.count()).select_from(model).where(*predicates)))
            rows = await session.scalars(
                select(model)
                .where(*predicates)
                .order_by(
                    model.started_at.desc(),
                    model.invocation_id,
                )
                .limit(max(1, min(limit, 10000)))
                .offset(max(0, offset))
            )
            return [self._value(row) for row in rows], total

    @staticmethod
    def _compact_columns():
        """SQLite projects small metadata instead of decoding entire receipt maps."""
        model = ExternalExecInvocationModel
        count = literal_column(
            "(SELECT COUNT(*) FROM json_each(external_exec_invocations.payload, '$.receipts'))"
        ).label("receipts_count")
        predicates = " OR ".join(
            f"json_extract(receipt.value, '$.usage.{field}') IS NOT NULL"
            for field in (
                "input_tokens",
                "cached_input_tokens",
                "cache_write_input_tokens",
                "output_tokens",
                "reasoning_output_tokens",
                "total_tokens",
            )
        )
        usage = literal_column(
            "(EXISTS(SELECT 1 FROM json_each(external_exec_invocations.payload, '$.receipts') AS receipt WHERE "
            + predicates
            + "))"
        ).label("usage_observed")
        return func.json_remove(model.payload, "$.receipts").label("metadata"), count, usage

    @staticmethod
    def _compact(row) -> ExternalExecInvocationCompact:
        data = json.loads(row.metadata)
        data.update(receipts_count=row.receipts_count, usage_observed=bool(row.usage_observed))
        return ExternalExecInvocationCompact.model_validate(data)

    async def get_page(
        self,
        invocation_id: str,
        *,
        receipt_offset: int = 0,
        receipt_limit: int = 100,
    ) -> ExternalExecInvocationCompact | None:
        model = ExternalExecInvocationModel
        receipt_limit = max(0, min(receipt_limit, 100))
        receipt_offset = max(0, receipt_offset)
        async with get_session(self._db_url) as session:
            await session.execute(text("BEGIN"))
            row = (
                await session.execute(
                    select(*self._compact_columns()).where(
                        model.invocation_id == invocation_id,
                    )
                )
            ).first()
            if row is None:
                return None
            value = self._compact(row)
            if receipt_limit:
                entries = (
                    func.json_each(model.payload, "$.receipts").table_valued("key", "value", "id").alias("receipt_page")
                )
                records = await session.execute(
                    select(entries.c.key, entries.c.value)
                    .select_from(model)
                    .join(
                        entries,
                        true(),
                    )
                    .where(model.invocation_id == invocation_id)
                    .order_by(entries.c.id)
                    .limit(receipt_limit)
                    .offset(receipt_offset)
                )
                value.receipts = {key: ExternalExecReceipt.model_validate_json(raw) for key, raw in records}
            value.receipts_offset = receipt_offset
            value.receipts_limit = receipt_limit
            value.receipts_truncated = receipt_offset + len(value.receipts) < value.receipts_count
            return value

    async def list_compact(
        self,
        *,
        project_id: str | None = None,
        source_namespace: str | None = None,
        since=None,
        until=None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[list[ExternalExecInvocationCompact], int]:
        model = ExternalExecInvocationModel
        conditions = []
        if project_id is not None:
            conditions.append(model.project_id == project_id)
        if source_namespace is not None:
            conditions.append(model.payload["source_namespace"].as_string() == source_namespace)
        if since is not None:
            conditions.append(model.started_at >= since)
        if until is not None:
            conditions.append(model.started_at <= until)
        async with get_session(self._db_url) as session:
            total = int(await session.scalar(select(func.count()).select_from(model).where(*conditions)))
            records = await session.execute(
                select(*self._compact_columns())
                .where(*conditions)
                .order_by(
                    model.started_at.desc(),
                    model.invocation_id,
                )
                .limit(max(1, min(limit, 500)))
                .offset(max(0, offset))
            )
            return [self._compact(row) for row in records], total

    async def mark_uploaded(self, invocation_id: str, revision: int) -> bool:
        """Only acknowledge the revision actually sent, never a concurrent append."""
        async with get_session(self._db_url) as session:
            await session.execute(text("BEGIN IMMEDIATE"))
            row = await session.get(ExternalExecInvocationModel, invocation_id)
            if row is None or revision > row.revision or revision < 1:
                return False
            row.uploaded_revision = max(row.uploaded_revision, revision)
            return True
