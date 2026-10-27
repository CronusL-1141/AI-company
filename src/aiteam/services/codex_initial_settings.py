"""Recognize only the native adjacent first-turn settings initialization pair."""

from datetime import datetime, timedelta

from aiteam.services.codex_usage_export import parse_usage_time


def row_time(row: dict) -> datetime | None:
    try:
        return parse_usage_time(row.get("timestamp"))
    except (ValueError, TypeError):
        return None


def initial_settings_match(
    row: dict, *, turn_at: datetime | None, session_id: str | None, model: str | None,
) -> bool:
    """No later setting, foreign thread, explicit null, or unknown model can bind.

    Callers retain ``turn_at`` for exactly the next JSONL record, and only for
    the first known turn before any usage and without pre-existing tier evidence.
    """
    if turn_at is None or not session_id or not model or row.get("type") != "event_msg":
        return False
    payload = row.get("payload")
    if not isinstance(payload, dict) or payload.get("type") != "thread_settings_applied":
        return False
    settings = payload.get("thread_settings")
    if (payload.get("thread_id") != session_id or not isinstance(settings, dict)
            or settings.get("model") != model):
        return False
    tier = settings.get("service_tier")
    at = row_time(row)
    return (isinstance(tier, str) and bool(tier.strip()) and at is not None
            and timedelta(0) <= at - turn_at <= timedelta(milliseconds=1))
