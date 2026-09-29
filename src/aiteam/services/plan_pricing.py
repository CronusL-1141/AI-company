"""Independent standard API-equivalent pricing for bound plan intervals.

The cycle arithmetic lives in ``plan_summary.advance_cycle``; the full-history
functions here fold it over a sorted list, and the storage layer applies the same
step incrementally (task 551aee38). ``pricing_sample_total`` is re-exported here for
its existing callers.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from decimal import Decimal, localcontext
from typing import Any, Literal

from aiteam.services.plan_summary import (
    CycleState,
    PricingMeta,
    advance_cycle,
    anchor_matches,
    fold_cycle,
    order_key,
    pricing_sample_total,
)
from aiteam.services.pricing import _precision
from aiteam.types import PricingPlanAnchor, PricingPlanCapacityEstimate, PricingPlanSnapshot

__all__ = [
    "estimate_from_cycle", "estimate_pricing_plan_capacity", "fold_window", "prediction_cycle_total",
    "pricing_sample_total", "window_status",
]


def prediction_cycle_total(snapshots: Sequence[PricingPlanSnapshot]) -> Decimal:
    """Return the current cycle's known main-bucket prices for capture storage."""
    keys = {(item.account_key, item.limit_id, item.window_duration_ms) for item in snapshots}
    if len(keys) != 1 or next(iter(keys))[1] != "codex":
        raise ValueError("prediction accumulation requires one account and attributed window")
    state, _ = fold_cycle(sorted(snapshots, key=order_key))
    return state.total if state is not None else Decimal(0)


def _result(
    latest: PricingPlanSnapshot, *, status: Literal["estimated", "collecting", "unavailable", "expired"],
    used_percent: int | None, reason: str | None = None,
    start_snapshot_id: str | None = None, interval_start: datetime | None = None,
    delta_usd: Decimal | None = None, delta_percent: int | None = None, total_usd: Decimal | None = None,
    pricing: PricingMeta | Any = None,
    assumed_tier_request_count: int | None = None, priced_request_count: int | None = None,
) -> PricingPlanCapacityEstimate:
    pricing = pricing or latest.pricing
    return PricingPlanCapacityEstimate(
        account_key=latest.account_key, limit_id=latest.limit_id,
        window_duration_ms=latest.window_duration_ms, resets_at=latest.resets_at,
        observed_at=latest.observed_at, used_percent=used_percent, status=status,
        source=latest.source, reason_code=reason, prediction_basis="cycle_anchor_missing_zero",
        estimated_total_usd=total_usd, delta_usd=delta_usd, delta_used_percent=delta_percent,
        start_snapshot_id=start_snapshot_id,
        end_snapshot_id=latest.snapshot_id,
        interval_start=interval_start,
        pricing_mode=pricing.pricing_mode if pricing is not None else None,
        catalog_version=pricing.catalog_version if pricing is not None else None,
        catalog_sha256=pricing.catalog_sha256 if pricing is not None else None,
        last_estimated_total_usd=total_usd,
        last_estimate_observed_at=latest.observed_at if total_usd is not None else None,
        assumed_tier_request_count=assumed_tier_request_count, priced_request_count=priced_request_count,
    )


def window_status(latest: PricingPlanSnapshot, now: datetime) -> PricingPlanCapacityEstimate | None:
    """The answer that only depends on the latest sample, or None to use the cycle."""
    if now >= latest.resets_at:
        return _result(latest, status="expired", used_percent=None)
    window_start = latest.resets_at - timedelta(milliseconds=latest.window_duration_ms)
    if now < window_start or not window_start <= latest.observed_at <= now:
        return _result(latest, status="unavailable", used_percent=None)
    if latest.limit_id != "codex":
        return _result(latest, status="unavailable", used_percent=latest.used_percent,
                       reason="bucket_activity_unattributed")
    return None


def estimate_from_cycle(latest: PricingPlanSnapshot, cycle: CycleState) -> PricingPlanCapacityEstimate:
    """Estimate the window from the cycle (automatic or manual) that ends at ``latest``."""
    delta_percent = latest.used_percent - cycle.baseline_used_percent
    reason = "pricing_incomplete" if cycle.assumed else cycle.reason
    fields = dict(used_percent=latest.used_percent, start_snapshot_id=cycle.baseline_id,
                  interval_start=cycle.baseline_observed_at, delta_usd=cycle.total,
                  delta_percent=delta_percent, pricing=cycle.pricing, reason=reason,
                  assumed_tier_request_count=cycle.assumed, priced_request_count=cycle.priced)
    if delta_percent <= 0:
        return _result(latest, status="collecting", **fields)
    with localcontext() as context:
        context.prec = _precision([cycle.total], extra_digits=16)
        total_usd = cycle.total * Decimal(100) / Decimal(delta_percent)
    return _result(latest, status="estimated", total_usd=total_usd, **fields)


def fold_window(
    ordered: Sequence[PricingPlanSnapshot], anchor: PricingPlanAnchor | None = None,
) -> tuple[CycleState | None, CycleState | None]:
    """Fold one ordered window: the automatic cycle and, if still valid, the manual one.

    A manual anchor starts its own cycle at the matching observation and stays valid
    only while the automatic cycle that contained it continues; the manual cycle
    re-counts from its anchor, so late prices for older requests remain outside it.
    """
    auto: CycleState | None = None
    manual: CycleState | None = None
    manual_cycle: str | None = None
    previous: PricingPlanSnapshot | None = None
    seen: set[str] = set()
    manual_seen: set[str] = set()
    for snapshot in ordered:
        previous_used = previous.used_percent if previous is not None else None
        step = advance_cycle(auto, previous_used, snapshot, seen)
        if step.reset:
            seen = set()
        seen.update(request_id for request_id, _ in step.counted)
        auto = step.state
        if manual is not None:
            if auto.baseline_id != manual_cycle:
                manual, manual_cycle = None, None
            else:
                manual_step = advance_cycle(manual, previous_used, snapshot, manual_seen)
                manual_seen.update(request_id for request_id, _ in manual_step.counted)
                manual = manual_step.state
        if anchor is not None and anchor_matches(snapshot, anchor):
            manual = advance_cycle(None, None, snapshot).state
            manual_cycle, manual_seen = auto.baseline_id, set()
        previous = snapshot
    return auto, manual


def _estimate_window(
    snapshots: list[PricingPlanSnapshot], now: datetime, anchor: PricingPlanAnchor | None = None,
) -> PricingPlanCapacityEstimate:
    ordered = sorted(snapshots, key=order_key)
    latest = ordered[-1]
    early = window_status(latest, now)
    if early is not None:
        return early
    auto, manual = fold_window(ordered, anchor)
    return estimate_from_cycle(latest, manual if manual is not None else auto)


def estimate_pricing_plan_capacity(
    snapshots: Sequence[PricingPlanSnapshot], *, now: datetime, anchors: Sequence[PricingPlanAnchor] = (),
) -> list[PricingPlanCapacityEstimate]:
    """Estimate from each cycle anchor, with missing prices contributing zero."""
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be a timezone-aware datetime")
    groups: dict[tuple[str, str, int], list[PricingPlanSnapshot]] = {}
    for snapshot in snapshots:
        validated = PricingPlanSnapshot.model_validate(snapshot.model_dump(mode="python"))
        if validated.pricing is not None:
            pricing_sample_total(validated.pricing)
        key = (validated.account_key, validated.limit_id, validated.window_duration_ms)
        groups.setdefault(key, []).append(validated)
    by_window = {}
    for anchor in anchors:
        anchor = PricingPlanAnchor.model_validate(anchor.model_dump(mode="python"))
        key = (anchor.account_key, anchor.limit_id, anchor.window_duration_ms)
        by_window[key] = anchor
    results = []
    for key in sorted(groups):
        group = groups[key]
        results.append(_estimate_window(group, now, by_window.get(key)))
    return results
