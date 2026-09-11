"""Pure metadata rules for the current automatic offline transition."""

from __future__ import annotations

from datetime import datetime, timedelta
from uuid import uuid4

from aiteam.clock import ensure_utc

AUTO_OFFLINE_KEY = "_aiteam_auto_offline"
AUTO_OFFLINE_REASONS = frozenset({"heartbeat_timeout", "config_liveness"})
MAX_SOURCE_OBSERVATION_AGE = timedelta(seconds=60)


def without_auto_offline(config: dict) -> dict:
    return {key: value for key, value in config.items() if key != AUTO_OFFLINE_KEY}


def with_auto_offline(config: dict, reason: str, occurred_at: datetime) -> dict:
    if reason not in AUTO_OFFLINE_REASONS or occurred_at.tzinfo is None:
        raise ValueError("invalid_auto_offline_transition")
    return {**without_auto_offline(config), AUTO_OFFLINE_KEY: {
        "version": 1, "reason": reason, "nonce": uuid4().hex,
        "occurred_at": ensure_utc(occurred_at).isoformat(),
    }}


def _parse_source_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return ensure_utc(parsed) if parsed.tzinfo is not None else None


def automatic_offline_at(config: dict) -> datetime | None:
    marker = config.get(AUTO_OFFLINE_KEY)
    if not isinstance(marker, dict) or set(marker) != {"version", "reason", "nonce", "occurred_at"}:
        return None
    if type(marker["version"]) is not int or marker["version"] != 1:
        return None
    if not isinstance(marker["reason"], str) or marker["reason"] not in AUTO_OFFLINE_REASONS:
        return None
    if not isinstance(marker["nonce"], str) or len(marker["nonce"]) != 32:
        return None
    return _parse_source_time(marker["occurred_at"])


def source_activity_time(value: object, now: datetime) -> tuple[datetime | None, str]:
    """Require source time; receiving an old event now cannot make it fresh."""
    if value is None:
        return None, "source_observed_at_missing"
    source_time = _parse_source_time(value)
    if source_time is None:
        return None, "source_observed_at_invalid"
    if source_time > now:
        return None, "source_observed_at_future"
    if now - source_time > MAX_SOURCE_OBSERVATION_AGE:
        return None, "source_observed_at_stale"
    return source_time, ""
