"""The single place where a timestamp crosses the storage boundary.

SQLite has no timezone-aware column type: SQLAlchemy's SQLite dialect renders a
``datetime`` to a plain ``'YYYY-MM-DD HH:MM:SS.ffffff'`` string and drops any
offset **silently** — passing an aware value and passing a naive one produce
byte-identical rows. ``DateTime(timezone=True)`` changes nothing here; it is a
no-op on this dialect. That silence is what let two wall clocks coexist in one
database for months without a single error being raised.

:class:`UtcDateTime` makes the conversion explicit and total:

* on the way in, an aware value is *converted* to UTC (not truncated) and then
  stored naive; a naive value is taken at face value as already-UTC;
* on the way out, the naive column value is labelled ``tzinfo=UTC``.

Two consequences follow, and both are the point of the design:

1. Everything above storage — services, API responses, the dashboard — sees
   timezone-aware UTC datetimes, so ``.isoformat()`` carries ``+00:00`` without
   any per-route serialization code.
2. Comparing a database value against a stray naive ``datetime.now()`` raises
   ``TypeError`` instead of returning an answer that is wrong by the host's UTC
   offset. The old failure mode was silent; this one cannot be missed.

The on-disk format is unchanged, so this type is transparent to existing rows —
only their *interpretation* is fixed, which is what the one-off value shift in
``scripts/migrate_timestamps_utc.py`` takes care of.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime
from sqlalchemy.types import TypeDecorator

from aiteam.clock import ensure_utc, to_naive_utc

__all__ = ["UtcDateTime"]


class UtcDateTime(TypeDecorator):
    """``DateTime`` column that always round-trips as timezone-aware UTC."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Any) -> datetime | None:
        if value is None:
            return None
        if not isinstance(value, datetime):  # let SQLAlchemy raise its own error
            return value
        return to_naive_utc(value)

    def process_result_value(self, value: Any, dialect: Any) -> datetime | None:
        if value is None:
            return None
        if not isinstance(value, datetime):
            return value
        return ensure_utc(value)
