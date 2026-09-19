"""Atomic, explicitly selected SQLite storage for native Codex usage facts."""

from __future__ import annotations

from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url

from aiteam.storage.connection import get_session
from aiteam.storage.engine_pool import engine_pool
from aiteam.storage.models import Base, CodexUsageObservationModel, CodexUsageSourceCursorModel
from aiteam.types import CodexUsageObservation, CodexUsageScanBatch, CodexUsageSourceCursor


class CodexUsageJournalRepository:
    """Never requires account rows or initializes unrelated application tables."""

    def __init__(self, db_url: str) -> None:
        if not isinstance(db_url, str) or not db_url.strip():
            raise ValueError("an explicit usage journal database URL is required")
        if make_url(db_url).get_backend_name() != "sqlite":
            raise ValueError("usage journaling requires an explicit SQLite database")
        self._db_url = db_url

    async def init_db(self) -> None:
        async with engine_pool.get_engine(self._db_url).begin() as connection:
            await connection.execute(text("BEGIN IMMEDIATE"))
            await connection.run_sync(Base.metadata.create_all, tables=[
                CodexUsageObservationModel.__table__, CodexUsageSourceCursorModel.__table__,
            ])

    async def get_cursor(self, source_id: str) -> CodexUsageSourceCursor | None:
        async with get_session(self._db_url) as session:
            row = await session.get(CodexUsageSourceCursorModel, source_id)
            return None if row is None else CodexUsageSourceCursor.model_validate(row.payload)

    async def list_cursors(self, *, limit: int = 1000) -> list[CodexUsageSourceCursor]:
        async with get_session(self._db_url) as session:
            rows = await session.scalars(select(CodexUsageSourceCursorModel).order_by(
                CodexUsageSourceCursorModel.source_id,
            ).limit(max(1, min(limit, 10000))))
            return [CodexUsageSourceCursor.model_validate(row.payload) for row in rows]

    async def list_observations(self, *, limit: int = 1000) -> list[CodexUsageObservation]:
        async with get_session(self._db_url) as session:
            rows = await session.scalars(select(CodexUsageObservationModel).order_by(
                CodexUsageObservationModel.saved_at, CodexUsageObservationModel.observation_id,
            ).limit(max(1, min(limit, 10000))))
            return [CodexUsageObservation.model_validate(row.payload) for row in rows]

    async def count_observations(self) -> int:
        async with get_session(self._db_url) as session:
            return int(await session.scalar(select(func.count()).select_from(CodexUsageObservationModel)))

    async def commit_batch(self, batch: CodexUsageScanBatch) -> bool:
        """Reject stale scans before inserting anything; facts and cursor commit together."""
        batch = CodexUsageScanBatch.model_validate(batch.model_dump(mode="python"))
        cursor = batch.cursor
        if cursor.revision != (batch.expected_revision or 0) + 1:
            raise ValueError("cursor revision must advance exactly once")
        for observation in batch.observations:
            if (
                observation.source_namespace != cursor.source_namespace
                or observation.source_id != cursor.source_id
                or observation.generation != cursor.generation
                or observation.byte_end > cursor.offset
            ):
                raise ValueError("journal facts must belong to their committed source range")
        async with get_session(self._db_url) as session:
            await session.execute(text("BEGIN IMMEDIATE"))
            row = await session.get(CodexUsageSourceCursorModel, cursor.source_id)
            actual_revision = None if row is None else row.revision
            if actual_revision != batch.expected_revision:
                return False
            if row is not None:
                previous = CodexUsageSourceCursor.model_validate(row.payload)
                if cursor.source_namespace != previous.source_namespace or (
                    cursor.generation < previous.generation
                    or (cursor.generation == previous.generation and cursor.offset < previous.offset)
                ):
                    raise ValueError("a cursor cannot rewind within its generation")
            for observation in batch.observations:
                if await session.get(CodexUsageObservationModel, observation.observation_id) is None:
                    session.add(CodexUsageObservationModel(
                        observation_id=observation.observation_id,
                        source_namespace=observation.source_namespace, source_id=observation.source_id,
                        saved_at=observation.saved_at, payload=observation.model_dump(mode="json"),
                    ))
                    await session.flush()
            if row is None:
                row = CodexUsageSourceCursorModel(source_id=cursor.source_id)
                session.add(row)
            row.source_namespace = cursor.source_namespace
            row.revision = cursor.revision
            row.payload = cursor.model_dump(mode="json")
            await session.flush()
            return True
