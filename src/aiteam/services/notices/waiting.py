"""What waits on the user: the one rule the Dashboard counts and lists (design §5.10).

The banner, the sidebar badge and the overview card (``GET /api/notices/summary``)
count these notices, and the notice tab of the pending page lists them, so the
count and the list cannot drift apart.

A notice waits on the user when its entry asks something of the user (kind
action or decision), is not a line shown on the spot (blocks, branch switches),
and the row is active or its snooze has ended. An ended snooze keeps the
snoozed status; the row simply waits again.
"""

from __future__ import annotations

from collections.abc import Collection
from datetime import datetime

from aiteam.services.notices.catalog import CATALOG, CatalogEntry
from aiteam.types import Notice, NoticeKind, NoticeStatus

# Rows not closed yet: active, or snoozed (possibly past its time).
OPEN_STATUSES = (NoticeStatus.ACTIVE.value, NoticeStatus.SNOOZED.value)
# E08 sums up the pending briefings, which are counted (and listed on the
# decisions tab) one by one; only the banner may show the aggregate itself.
BRIEFING_AGGREGATES = frozenset({"decisions_pending"})


def _asks_the_user(entry: CatalogEntry) -> bool:
    return (entry.ledger and entry.kind in (NoticeKind.ACTION, NoticeKind.DECISION)
            and "immediate" not in entry.render_at)


# Entries the banner may show.
BANNER_IDS = frozenset(entry.id for entry in CATALOG.values() if _asks_the_user(entry))
# Entries counted and listed as notices.
WAITING_IDS = BANNER_IDS - BRIEFING_AGGREGATES


def snoozed(notice: Notice, now: datetime) -> bool:
    """True while a snooze still holds the notice back."""
    return notice.status == NoticeStatus.SNOOZED and (
        notice.snoozed_until is None or notice.snoozed_until > now)


async def list_waiting(
    repo,
    now: datetime,
    *,
    with_briefing_aggregate: bool = False,
    catalog_ids: Collection[str] | None = None,
    project_ids: Collection[str] | None = None,
    limit: int | None = None,
    offset: int = 0,
) -> tuple[list[Notice], int]:
    """Waiting notices, newest first, and how many there are in all.

    ``catalog_ids`` narrows the rule (the list's kind and group filters) and
    never widens it. The snooze check runs before paging, so pages and
    ``total`` agree.
    """
    ids = BANNER_IDS if with_briefing_aggregate else WAITING_IDS
    if catalog_ids is not None:
        ids = ids & frozenset(catalog_ids)
    rows, _ = await repo.list_notices(statuses=OPEN_STATUSES, catalog_ids=ids, project_ids=project_ids)
    awake = [row for row in rows if not snoozed(row, now)]
    end = None if limit is None else offset + limit
    return awake[offset:end], len(awake)
