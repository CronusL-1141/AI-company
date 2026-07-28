"""AI Team OS — the single clock.

One rule, no exceptions: **every timestamp in this system is UTC**.

Why this module exists
----------------------
Before 2026-07-28 the repository ran two wall clocks side by side. The core
domain wrote ``datetime.now()`` (host-local, naive) while the ecosystem domain
wrote ``datetime.now(tz=UTC)``. Both were internally consistent, so each domain
looked correct in isolation — the damage only showed up where a cutoff from one
domain was compared against a column from the other, which silently shifted the
answer by the host's UTC offset without ever raising. Three such comparisons
were found and fixed in a single sweep, which is what proved the split was not
a design but a slow leak.

The rules
---------
1. Anything that needs "now" calls :func:`utc_now`. Never ``datetime.now()``.
2. Anything derived from a POSIX timestamp (file mtime, epoch millis from an
   external journal) goes through :func:`from_timestamp` / :func:`from_epoch_ms`
   so the instant is *labelled* UTC rather than reinterpreted as local.
3. Anything parsed from a string goes through :func:`parse_utc`, which treats a
   bare (offset-less) string as UTC — that is the storage convention.
4. Storage attaches/strips the offset in exactly one place
   (``aiteam.storage.models.UtcDateTime``); no caller ever does it by hand.

Because every value above the storage boundary is timezone-aware, mixing a
stray naive datetime into a comparison now raises ``TypeError`` instead of
quietly returning a wrong answer. That noisy failure is the point: it makes the
old bug class structurally unrepresentable.

This module deliberately depends on nothing but the standard library — the hook
scripts import it while running outside the API process.
"""

from __future__ import annotations

from datetime import UTC, datetime

__all__ = [
    "utc_now",
    "naive_utc_now",
    "ensure_utc",
    "to_naive_utc",
    "from_timestamp",
    "from_epoch_ms",
    "parse_utc",
]


def utc_now() -> datetime:
    """Current instant as a timezone-aware UTC datetime.

    The default "now" for the whole system. Serializes as
    ``2026-07-28T09:00:00+00:00`` — the offset is what makes the value
    self-describing on the wire.
    """
    return datetime.now(tz=UTC)


def naive_utc_now() -> datetime:
    """Current instant as a *naive* datetime whose value is UTC.

    Only for the handful of places that must compare against raw column values
    read outside the ORM (ad-hoc SQL, migration scripts). Application code
    should use :func:`utc_now`.
    """
    return datetime.now(tz=UTC).replace(tzinfo=None)


def ensure_utc(value: datetime | None) -> datetime | None:
    """Return *value* as aware UTC; a naive input is *assumed* to already be UTC.

    The assumption is the storage convention: SQLite has no tz-aware type, so
    persisted values are naive-UTC by construction. Anything arriving naive from
    the database therefore needs labelling, not conversion.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def to_naive_utc(value: datetime | None) -> datetime | None:
    """Return *value* as a naive datetime holding the UTC instant.

    The storage-side counterpart of :func:`ensure_utc`. An aware input is
    converted to UTC first, so a value that arrives with a ``+08:00`` offset is
    shifted (not truncated) before the offset is dropped.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


def from_timestamp(ts: float) -> datetime:
    """POSIX timestamp -> aware UTC datetime.

    Replaces bare ``datetime.fromtimestamp(ts)``, which labels the instant with
    the host's local offset and so re-enters the value into the wrong clock.
    """
    return datetime.fromtimestamp(ts, tz=UTC)


def from_epoch_ms(ms: float) -> datetime:
    """Epoch milliseconds (Claude Code journals use these) -> aware UTC."""
    return datetime.fromtimestamp(ms / 1000.0, tz=UTC)


def parse_utc(raw: str | None) -> datetime | None:
    """Parse an ISO-8601 string to aware UTC; ``None`` when unparseable.

    Accepts a trailing ``Z``, an explicit offset, or no offset at all. A string
    without an offset is read as UTC, matching what storage writes.
    """
    if not raw:
        return None
    text = raw.strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    return ensure_utc(parsed)
