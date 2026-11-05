"""Atomic, explicitly selected SQLite storage for native Codex usage facts."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime

from sqlalchemy import bindparam, func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError

from aiteam.clock import to_naive_utc
from aiteam.storage.connection import get_session
from aiteam.storage.engine_pool import engine_pool
from aiteam.storage.models import Base, CodexUsageObservationModel, CodexUsageSourceCursorModel
from aiteam.types import CodexUsageObservation, CodexUsageScanBatch, CodexUsageSourceCursor

_LOOKUP_CHUNK = 400

# (seq, source_id, payload JSON text): parsed and validated by the caller off the event loop.
JournalRow = tuple[int, str, str]


class CodexUsageJournalUnavailableError(RuntimeError):
    """The journal could not be read; infrastructure, never an empty result.

    Deliberately not a ValueError: a prediction round that meets it saves nothing
    and retries, instead of recording a break in its usage segment.
    """


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
        """Reject stale scans before inserting anything; facts and cursor commit together.

        New facts are numbered in recording order inside the write reservation,
        so the numbering has no gaps or races between writers.
        """
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
            seq = None
            for observation in batch.observations:
                if await session.get(CodexUsageObservationModel, observation.observation_id) is None:
                    if seq is None:
                        seq = int(await session.scalar(select(func.coalesce(
                            func.max(CodexUsageObservationModel.seq), 0,
                        ))))
                    seq += 1
                    session.add(CodexUsageObservationModel(
                        observation_id=observation.observation_id,
                        source_namespace=observation.source_namespace, source_id=observation.source_id,
                        saved_at=observation.saved_at, payload=observation.model_dump(mode="json"),
                        seq=seq, kind=observation.kind, occurred_at=observation.occurred_at,
                        response_id=observation.response_id, session_id=observation.session_id,
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

    # ------------------------------------------------------------ projection reads
    # Bounded, indexed reads for the prediction projection. Each returns raw payload
    # text: decoding thousands of facts belongs off the event loop. Every read is
    # bounded by a recording position, so later writes cannot change its answer.

    async def _rows(self, statement, values: dict) -> list:
        try:
            async with get_session(self._db_url) as session:
                return list((await session.execute(statement, values)).all())
        except DBAPIError as error:
            raise CodexUsageJournalUnavailableError(type(error.orig).__name__) from error

    async def recorded_through(self) -> int:
        """The latest recording position; 0 for an empty journal."""
        rows = await self._rows(text("SELECT COALESCE(MAX(seq), 0) FROM codex_usage_observations"), {})
        return int(rows[0][0])

    async def consumable(
        self, *, after: int, through: int, since: datetime, until: datetime, binding: datetime,
    ) -> list[JournalRow]:
        """Facts first consumable in ``(since, until]`` after consuming ``(after, since]``.

        Recorded in ``(after, through]`` and not occurring after ``until``, or
        recorded by ``after`` but occurring in ``(since, until]`` (they were not
        yet due). Either way they occurred after ``binding``; a fact without a
        valid time is consumed once, by its recording position.
        """
        statement = text(
            "SELECT seq, source_id, payload FROM codex_usage_observations "
            "WHERE (seq > :after AND seq <= :through AND (occurred_at IS NULL "
            "       OR (occurred_at > :binding AND occurred_at <= :until))) "
            "   OR (seq <= :after AND occurred_at > :since AND occurred_at <= :until AND occurred_at > :binding) "
            "ORDER BY seq"
        )
        return await self._rows(statement, {
            "after": after, "through": through, "since": _stored(since),
            "until": _stored(until), "binding": _stored(binding),
        })

    async def ledger_occurrences(self, response_ids: Iterable[str], *, through: int) -> list[JournalRow]:
        """Every recorded ledger fact of these responses up to ``through``."""
        statement = text(
            "SELECT seq, source_id, payload FROM codex_usage_observations "
            "WHERE kind = 'ledger' AND seq <= :through AND response_id IN :ids ORDER BY seq",
        ).bindparams(bindparam("ids", expanding=True))
        return await self._chunked(statement, response_ids, through=through)

    async def session_facts(self, session_ids: Iterable[str], *, kind: str, through: int) -> list[JournalRow]:
        """Every recorded fact of one kind for these sessions up to ``through``."""
        statement = text(
            "SELECT seq, source_id, payload FROM codex_usage_observations "
            "WHERE session_id IN :ids AND kind = :kind AND seq <= :through ORDER BY seq",
        ).bindparams(bindparam("ids", expanding=True))
        return await self._chunked(statement, session_ids, through=through, kind=kind)

    async def ledger_sessions(self, session_ids: Iterable[str], *, through: int) -> set[str]:
        """The sessions among these with at least one recorded ledger fact."""
        statement = text(
            "SELECT DISTINCT session_id FROM codex_usage_observations "
            "WHERE session_id IN :ids AND kind = 'ledger' AND seq <= :through",
        ).bindparams(bindparam("ids", expanding=True))
        return {row[0] for row in await self._chunked(statement, session_ids, through=through)}

    async def ledger_sources(self, source_ids: Iterable[str], *, through: int) -> set[str]:
        """The physical sources among these with at least one recorded ledger fact."""
        statement = text(
            "SELECT DISTINCT source_id FROM codex_usage_observations "
            "WHERE source_id IN :ids AND kind = 'ledger' AND seq <= :through",
        ).bindparams(bindparam("ids", expanding=True))
        return {row[0] for row in await self._chunked(statement, source_ids, through=through)}

    async def _chunked(self, statement, keys: Iterable[str], **values) -> list:
        ordered = sorted({key for key in keys if key})
        rows: list = []
        for offset in range(0, len(ordered), _LOOKUP_CHUNK):
            rows.extend(await self._rows(statement, {**values, "ids": ordered[offset:offset + _LOOKUP_CHUNK]}))
        return rows


def _stored(value: datetime) -> str:
    """The text a UtcDateTime column holds for ``value``, for raw comparisons."""
    return to_naive_utc(value).strftime("%Y-%m-%d %H:%M:%S.%f")
