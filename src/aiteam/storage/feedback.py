"""The feedback ledger (``feedback_submissions``): drafts, what was published, how it was confirmed.

Writes touch this node's own rows only (aiteam.storage.partition): the center holds
its edges' rows too, and they are theirs to change. Reads see every row.

Publishing starts with a claim, one write-locked transaction: the node's live rows
of the last 24 hours are counted against the rate limit, then the draft moves to
``sending`` and takes ``claim_hash``. The partial unique index on (target,
claim_hash) lets one row hold a text at a time: a second claim of the same text,
from any process, fails there.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from aiteam.clock import utc_now
from aiteam.storage.connection import begin_write, get_session
from aiteam.storage.models import FeedbackSubmissionModel as Row
from aiteam.storage.partition import own
from aiteam.types import (
    FEEDBACK_LIVE_STATUSES,
    FeedbackKind,
    FeedbackStatus,
    FeedbackSubmission,
)

_LIVE = [str(status) for status in FEEDBACK_LIVE_STATUSES]


class Duplicate(Exception):  # noqa: N818 - an outcome the caller answers with the existing post
    """The same text is already published, or being published, from this node."""

    def __init__(self, existing: FeedbackSubmission | None) -> None:
        super().__init__("duplicate")
        self.existing = existing


class RateLimited(Exception):  # noqa: N818
    def __init__(self, count: int, limit: int) -> None:
        super().__init__(f"{count} >= {limit}")
        self.count, self.limit = count, limit


class NotClaimable(Exception):  # noqa: N818
    """The draft is gone, someone else's, or no longer a draft (``status``)."""

    def __init__(self, status: str) -> None:
        super().__init__(status)
        self.status = status


def _submission(row: Row) -> FeedbackSubmission:
    return FeedbackSubmission.model_validate(row, from_attributes=True)


def _claim(status: FeedbackStatus, content_hash: str) -> str:
    return content_hash if status in FEEDBACK_LIVE_STATUSES else ""


class FeedbackRepository:
    """The ledger of one database."""

    def __init__(self, db_url: str | None) -> None:
        self._db_url = db_url

    async def insert(self, **values: Any) -> FeedbackSubmission:
        status = FeedbackStatus(values["status"])
        now = utc_now()
        row = Row(**{**values, "status": str(status), "claim_hash": _claim(status, values["content_hash"]),
                     "created_at": values.get("created_at") or now, "updated_at": now})
        async with get_session(self._db_url) as session:
            session.add(row)
            await session.flush()
            return _submission(row)

    async def get(self, draft_id: str) -> FeedbackSubmission | None:
        async with get_session(self._db_url) as session:
            row = await session.get(Row, draft_id)
            return _submission(row) if row is not None else None

    async def holding(self, target_repo: str, content_hash: str) -> list[FeedbackSubmission]:
        """Rows of any node that hold this text live (sending, sent, unknown), newest first."""
        return await self._select(Row.target_repo == target_repo, Row.content_hash == content_hash,
                                  Row.status.in_(_LIVE))

    async def latest(self, target_repo: str, content_hash: str, status: FeedbackStatus) -> FeedbackSubmission | None:
        rows = await self._select(Row.target_repo == target_repo, Row.content_hash == content_hash,
                                  Row.status == str(status), limit=1)
        return rows[0] if rows else None

    async def claim(self, draft_id: str, *, since: datetime, limit: int, now: datetime,
                    values: dict[str, Any]) -> FeedbackSubmission:
        """Move this node's draft to sending (see the module doc); ``values`` are the confirmation's columns."""
        try:
            async with get_session(self._db_url) as session:
                await begin_write(session)
                count = (await session.execute(select(func.count()).select_from(Row).where(
                    own(Row.origin_node), Row.status.in_(_LIVE), Row.sent_at >= since))).scalar_one()
                if count >= limit:
                    raise RateLimited(count, limit)
                result = await session.execute(update(Row).where(
                    Row.id == draft_id, Row.status == str(FeedbackStatus.DRAFT), own(Row.origin_node),
                ).values(status=str(FeedbackStatus.SENDING), claim_hash=Row.content_hash, sent_at=now,
                         updated_at=now, **values))
                if result.rowcount == 0:
                    current = await session.get(Row, draft_id)
                    raise NotClaimable(current.status if current is not None else "missing")
                return _submission(await session.get(Row, draft_id, populate_existing=True))
        except IntegrityError:
            draft = await self.get(draft_id)
            held = await self.holding(draft.target_repo, draft.content_hash) if draft is not None else []
            raise Duplicate(held[0] if held else None) from None

    async def update(self, draft_id: str, status: FeedbackStatus, **values: Any) -> FeedbackSubmission | None:
        """Set this node's row to ``status`` (and ``values``); None when it is not this node's."""
        async with get_session(self._db_url) as session:
            mine = select(Row).where(Row.id == draft_id, own(Row.origin_node))
            row = (await session.execute(mine)).scalar_one_or_none()
            if row is None:
                return None
            for key, value in {**values, "status": str(status), "claim_hash": _claim(status, row.content_hash),
                               "updated_at": utc_now()}.items():
                setattr(row, key, value)
            await session.flush()
            return _submission(row)

    async def expire_drafts(self, created_before: datetime) -> int:
        async with get_session(self._db_url) as session:
            result = await session.execute(update(Row).where(
                own(Row.origin_node), Row.status == str(FeedbackStatus.DRAFT), Row.created_at < created_before,
            ).values(status=str(FeedbackStatus.EXPIRED), updated_at=utc_now()))
            return result.rowcount or 0

    async def unsettled(self, sending_before: datetime, limit: int) -> list[FeedbackSubmission]:
        """This node's rows whose fate is open: unknown, or sending since before ``sending_before``."""
        return await self._select(
            own(Row.origin_node),
            (Row.status == str(FeedbackStatus.UNKNOWN))
            | ((Row.status == str(FeedbackStatus.SENDING)) & (Row.sent_at < sending_before)),
            limit=limit)

    async def count_live_since(self, since: datetime) -> int:
        async with get_session(self._db_url) as session:
            return (await session.execute(select(func.count()).select_from(Row).where(
                own(Row.origin_node), Row.status.in_(_LIVE), Row.sent_at >= since))).scalar_one()

    async def recent(self, status: FeedbackStatus | None, limit: int) -> list[FeedbackSubmission]:
        return await self._select(*([Row.status == str(status)] if status else []), limit=limit)

    async def sent_titles(self, kind: FeedbackKind, since: datetime, limit: int) -> list[FeedbackSubmission]:
        return await self._select(Row.kind == str(kind), Row.status == str(FeedbackStatus.SENT),
                                  Row.sent_at >= since, limit=limit)

    async def _select(self, *conditions: Any, limit: int | None = None) -> list[FeedbackSubmission]:
        statement = select(Row).where(*conditions).order_by(Row.created_at.desc())
        if limit is not None:
            statement = statement.limit(limit)
        async with get_session(self._db_url) as session:
            return [_submission(row) for row in (await session.execute(statement)).scalars()]
