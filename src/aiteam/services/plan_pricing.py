"""Independent standard API-equivalent pricing for bound plan intervals."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from datetime import datetime, timedelta
from decimal import Decimal, localcontext
from typing import Literal

from aiteam.services.pricing import _precision, _request_amount
from aiteam.types import PricingPlanAnchor, PricingPlanCapacityEstimate, PricingPlanSample, PricingPlanSnapshot


def pricing_sample_total(sample: PricingPlanSample) -> Decimal | None:
    """Verify persisted request prices and return only a complete interval sum."""
    sample = PricingPlanSample.model_validate(sample.model_dump(mode="python"))
    requests = {entry.request.request_id: entry.request for entry in sample.entries}
    for quote in sample.quotes:
        for item in quote.items:
            if item.status != "priced":
                continue
            request, record = requests[item.request_id], item.rate_record
            if (
                (request.cached_input_tokens and record.rates.cached_input is None)
                or (request.cache_write_input_tokens and record.rates.cache_write is None)
                or _request_amount(request, record) != item.amount_usd
            ):
                raise ValueError("persisted request amount does not match its exact rate record")
    if not sample.complete:
        return None
    amounts = [quote.total_usd for quote in sample.quotes]
    with localcontext() as context:
        context.prec = _precision(amounts)
        return sum(amounts, Decimal(0))


def _cycle_points(snapshots: Sequence[PricingPlanSnapshot]) -> Iterator[tuple[
    PricingPlanSnapshot, PricingPlanSnapshot, Decimal, PricingPlanSample | None, str | None,
]]:
    """Rebuild known contributions without modifying old immutable snapshots.

    A cycle starts at its earliest quota observation. Source changes, missing
    samples and catalog changes do not move that anchor. Stored per-request
    quotes remain authoritative, and overlapping intervals count each ID once.
    """
    previous = baseline = pricing = None
    total = Decimal(0)
    seen: set[str] = set()
    reason = None
    for snapshot in sorted(snapshots, key=lambda item: (item.observed_at, item.snapshot_id)):
        if previous is None or (
            snapshot.resets_at != previous.resets_at or snapshot.used_percent < previous.used_percent
        ):
            baseline, pricing, total, reason = snapshot, None, Decimal(0), None
            seen.clear()
        sample = snapshot.pricing
        if sample is None:
            reason = "pricing_unavailable"
        else:
            pricing_sample_total(sample)
            pricing = sample
            if not sample.complete:
                reason = "pricing_incomplete"
            if snapshot is not baseline:
                requests = {entry.request.request_id: entry for entry in sample.entries}
                amounts = [total]
                for quote in sample.quotes:
                    for item in quote.items:
                        if (item.status != "priced" or item.request_id in seen
                                or not baseline.observed_at < requests[item.request_id].occurred_at
                                or "gpt-5.3-codex-spark" in (item.model, item.canonical_model)):
                            continue
                        seen.add(item.request_id)
                        amounts.append(item.amount_usd)
                with localcontext() as context:
                    context.prec = _precision(amounts)
                    total = sum(amounts, Decimal(0))
        yield snapshot, baseline, total, pricing, reason
        previous = snapshot


def prediction_cycle_total(snapshots: Sequence[PricingPlanSnapshot]) -> Decimal:
    """Return the current cycle's known main-bucket prices for capture storage."""
    keys = {(item.account_key, item.limit_id, item.window_duration_ms) for item in snapshots}
    if len(keys) != 1 or next(iter(keys))[1] != "codex":
        raise ValueError("prediction accumulation requires one account and attributed window")
    total = Decimal(0)
    for _, _, total, _, _ in _cycle_points(snapshots):
        pass
    return total


def _result(
    latest: PricingPlanSnapshot, *, status: Literal["estimated", "collecting", "unavailable", "expired"],
    used_percent: int | None, reason: str | None = None,
    baseline: PricingPlanSnapshot | None = None, delta_usd: Decimal | None = None,
    delta_percent: int | None = None, total_usd: Decimal | None = None,
    pricing: PricingPlanSample | None = None,
) -> PricingPlanCapacityEstimate:
    pricing = pricing or latest.pricing
    return PricingPlanCapacityEstimate(
        account_key=latest.account_key, limit_id=latest.limit_id,
        window_duration_ms=latest.window_duration_ms, resets_at=latest.resets_at,
        observed_at=latest.observed_at, used_percent=used_percent, status=status,
        source=latest.source, reason_code=reason, prediction_basis="cycle_anchor_missing_zero",
        estimated_total_usd=total_usd, delta_usd=delta_usd, delta_used_percent=delta_percent,
        start_snapshot_id=baseline.snapshot_id if baseline is not None else None,
        end_snapshot_id=latest.snapshot_id,
        interval_start=baseline.observed_at if baseline is not None else None,
        pricing_mode=pricing.pricing_mode if pricing is not None else None,
        catalog_version=pricing.catalog_version if pricing is not None else None,
        catalog_sha256=pricing.catalog_sha256 if pricing is not None else None,
        last_estimated_total_usd=total_usd,
        last_estimate_observed_at=latest.observed_at if total_usd is not None else None,
    )


def _estimate_window(
    snapshots: list[PricingPlanSnapshot], now: datetime, anchor: PricingPlanAnchor | None = None,
) -> PricingPlanCapacityEstimate:
    ordered = sorted(snapshots, key=lambda item: (item.observed_at, item.snapshot_id))
    latest = ordered[-1]
    if now >= latest.resets_at:
        return _result(latest, status="expired", used_percent=None)
    window_start = latest.resets_at - timedelta(milliseconds=latest.window_duration_ms)
    if now < window_start or not window_start <= latest.observed_at <= now:
        return _result(latest, status="unavailable", used_percent=None)
    if latest.limit_id != "codex":
        return _result(latest, status="unavailable", used_percent=latest.used_percent,
                       reason="bucket_activity_unattributed")
    manual_baseline = manual_cycle = None
    for snapshot, baseline, delta_usd, pricing, reason in _cycle_points(ordered):
        if manual_cycle is not None and baseline.snapshot_id != manual_cycle:
            manual_baseline = manual_cycle = None
        if anchor is not None and all(getattr(snapshot, field) == getattr(anchor, field) for field in (
            "snapshot_id", "account_key", "limit_id", "window_duration_ms", "resets_at", "observed_at", "used_percent",
        )):
            manual_baseline, manual_cycle = snapshot, baseline.snapshot_id
    if manual_baseline is not None:
        # Rebuild the manual interval so late prices for older requests remain outside it.
        boundary = (manual_baseline.observed_at, manual_baseline.snapshot_id)
        manual_points = [item for item in ordered if (item.observed_at, item.snapshot_id) >= boundary]
        for _, baseline, delta_usd, pricing, reason in _cycle_points(manual_points):
            pass
    delta_percent = latest.used_percent - baseline.used_percent
    fields = dict(used_percent=latest.used_percent, baseline=baseline, delta_usd=delta_usd,
                  delta_percent=delta_percent, pricing=pricing, reason=reason)
    if delta_percent <= 0:
        return _result(latest, status="collecting", **fields)
    with localcontext() as context:
        context.prec = _precision([delta_usd], extra_digits=16)
        total_usd = delta_usd * Decimal(100) / Decimal(delta_percent)
    return _result(latest, status="estimated", total_usd=total_usd, **fields)


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
