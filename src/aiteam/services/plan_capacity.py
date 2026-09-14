"""Infer plan capacity from continuous native activity and quota observations."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Literal

from aiteam.types import PlanCapacityEstimate, PlanUsageSnapshot

_MAX_SAFE_INTEGER = 9_007_199_254_740_991
_MIN_INTERVAL = timedelta(seconds=300)


def _result(
    latest: PlanUsageSnapshot,
    *,
    status: Literal["estimated", "collecting", "unavailable", "expired"],
    used_percent: int | None,
    baseline: PlanUsageSnapshot | None = None,
    delta_tokens: int | None = None,
    delta_percent: int | None = None,
    total_tokens: int | None = None,
) -> PlanCapacityEstimate:
    return PlanCapacityEstimate(
        account_key=latest.account_key, limit_id=latest.limit_id,
        window_duration_ms=latest.window_duration_ms, resets_at=latest.resets_at,
        observed_at=latest.observed_at, used_percent=used_percent,
        estimated_total_tokens=total_tokens, delta_tokens=delta_tokens,
        delta_used_percent=delta_percent,
        start_snapshot_id=baseline.snapshot_id if baseline is not None else None,
        end_snapshot_id=latest.snapshot_id,
        interval_start=baseline.observed_at if baseline is not None else None,
        status=status, source=latest.source,
    )


def _estimate_window(snapshots: list[PlanUsageSnapshot], now: datetime) -> PlanCapacityEstimate:
    ordered = sorted(snapshots, key=lambda snapshot: (snapshot.observed_at, snapshot.snapshot_id))
    latest = ordered[-1]
    if now >= latest.resets_at:
        return _result(latest, status="expired", used_percent=None)
    window_start = latest.resets_at - timedelta(milliseconds=latest.window_duration_ms)
    if now < window_start or not window_start <= latest.observed_at <= now:
        return _result(latest, status="unavailable", used_percent=None)
    if latest.limit_id != "codex":
        # An account-wide counter does not identify a separate model bucket.
        return _result(latest, status="unavailable", used_percent=latest.used_percent)

    baseline: PlanUsageSnapshot | None = None
    previous: PlanUsageSnapshot | None = None
    for snapshot in ordered:
        if snapshot.observed_at < window_start or snapshot.activity_tokens is None:
            baseline = None
            previous = None
            continue
        if (
            previous is None
            or snapshot.source != previous.source
            or snapshot.activity_scope != previous.activity_scope
            or snapshot.observed_at <= previous.observed_at
            or snapshot.activity_observed_at <= previous.activity_observed_at
            or snapshot.activity_tokens < previous.activity_tokens
            or snapshot.used_percent < previous.used_percent
        ):
            baseline = snapshot
        previous = snapshot

    if baseline is None or baseline is latest:
        return _result(latest, status="collecting", used_percent=latest.used_percent)

    delta_tokens = latest.activity_tokens - baseline.activity_tokens
    delta_percent = latest.used_percent - baseline.used_percent
    fields = {
        "used_percent": latest.used_percent, "baseline": baseline,
        "delta_tokens": delta_tokens, "delta_percent": delta_percent,
    }
    if (
        latest.observed_at - baseline.observed_at < _MIN_INTERVAL
        or delta_percent <= 1
        or delta_tokens <= 0
    ):
        return _result(latest, status="collecting", **fields)

    total_tokens = 100 * delta_tokens // delta_percent
    if total_tokens > _MAX_SAFE_INTEGER:
        return _result(latest, status="unavailable", **fields)
    return _result(latest, status="estimated", total_tokens=total_tokens, **fields)


def estimate_plan_capacity(
    snapshots: Sequence[PlanUsageSnapshot], *, now: datetime,
) -> list[PlanCapacityEstimate]:
    """Estimate each account and quota window using its latest reset segment.

    Every input is independently validated, including already-created model
    instances. Missing measurements reset the baseline rather than becoming zero.
    Integer arithmetic preserves the floor and the JavaScript-safe output bound.
    """
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be a timezone-aware datetime")
    groups: dict[tuple[str, str, int], list[PlanUsageSnapshot]] = {}
    for snapshot in snapshots:
        validated = PlanUsageSnapshot.model_validate(snapshot.model_dump(mode="python"))
        group = (validated.account_key, validated.limit_id, validated.window_duration_ms)
        groups.setdefault(group, []).append(validated)
    results: list[PlanCapacityEstimate] = []
    for key in sorted(groups):
        group = groups[key]
        latest_reset = max(group, key=lambda snapshot: (snapshot.observed_at, snapshot.snapshot_id)).resets_at
        window = [snapshot for snapshot in group if snapshot.resets_at == latest_reset]
        results.append(_estimate_window(window, now))
    return results
