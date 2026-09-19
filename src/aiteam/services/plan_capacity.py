"""Estimate plan capacity from bounded local samples and native percentages."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Literal

from aiteam.types import PlanCapacityEstimate, PlanUsageSnapshot

_MAX_SAFE_INTEGER = 9_007_199_254_740_991


def _result(
    latest: PlanUsageSnapshot,
    *,
    status: Literal["estimated", "collecting", "unavailable", "expired"],
    used_percent: int | None,
    reason_code: Literal[
        "activity_coverage_unknown", "bucket_activity_unattributed", "local_usage_unavailable",
    ] | None = None,
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
        status=status, source=latest.source, reason_code=reason_code,
    )


def _is_local_sample(snapshot: PlanUsageSnapshot) -> bool:
    return (
        snapshot.source == "codex_local_logs"
        and snapshot.activity_tokens is not None
        and snapshot.activity_scope is not None
        and snapshot.activity_binding_at is not None
        and snapshot.activity_binding_at <= snapshot.observed_at
        and snapshot.activity_observed_at == snapshot.observed_at
    )


def _estimate_local_window(
    ordered: list[PlanUsageSnapshot], window_start: datetime,
) -> PlanCapacityEstimate:
    latest = ordered[-1]
    if not _is_local_sample(latest):
        return _result(
            latest, status="unavailable", used_percent=latest.used_percent,
            reason_code="local_usage_unavailable",
        )
    baseline: PlanUsageSnapshot | None = None
    previous: PlanUsageSnapshot | None = None
    for snapshot in ordered:
        if snapshot.observed_at < window_start or not _is_local_sample(snapshot):
            baseline = None
            previous = None
            continue
        if (
            previous is None
            or snapshot.activity_scope != previous.activity_scope
            or snapshot.activity_binding_at != previous.activity_binding_at
            or snapshot.observed_at <= previous.observed_at
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
        latest.observed_at <= baseline.observed_at
        or delta_percent <= 0 or delta_tokens <= 0
    ):
        return _result(latest, status="collecting", **fields)
    total_tokens = 100 * delta_tokens // delta_percent
    if total_tokens > _MAX_SAFE_INTEGER:
        return _result(latest, status="unavailable", **fields)
    return _result(latest, status="estimated", total_tokens=total_tokens, **fields)


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
        return _result(
            latest, status="unavailable", used_percent=latest.used_percent,
            reason_code="bucket_activity_unattributed",
        )

    if latest.source == "codex_local_logs":
        return _estimate_local_window(ordered, window_start)

    # The native summary has no provider coverage boundary. Receipt times and
    # date-set hashes cannot align a delayed counter with live quota usage.
    return _result(
        latest, status="unavailable", used_percent=latest.used_percent,
        reason_code="activity_coverage_unknown",
    )


def estimate_plan_capacity(
    snapshots: Sequence[PlanUsageSnapshot], *, now: datetime,
) -> list[PlanCapacityEstimate]:
    """Return each account's latest quota window with its evidence boundary.

    Every input is independently validated, including already-created model
    instances. Local samples need a continuous binding inside one quota window;
    older account summaries never acquire coverage evidence from receipt times.
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
        # ``resets_at`` is provider scheduling metadata and can move between
        # reads. A new local cycle is established only by the account usage
        # reading rolling back (handled in _estimate_local_window).
        results.append(_estimate_window(group, now))
    return results
