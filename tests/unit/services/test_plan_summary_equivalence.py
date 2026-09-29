"""The step functions reproduce the full-history code exactly (task 551aee38).

The oracle is a4bab96's plan_pricing / plan_capacity, frozen verbatim in
tests/unit/_oracle_*.py. Every comparison is exact: the cycle total's Decimal string
(it is stored), every estimate field (JSON dump), including the counts and the
pricing metadata. A production copy (18,449 price and 18,518 plan rows) was
replayed through the same comparison when this was written; see the task report.
"""

from __future__ import annotations

import random
from datetime import timedelta

import pytest

from aiteam.services import plan_capacity, plan_pricing
from aiteam.services.plan_summary import advance_cycle, advance_run_start, fold_cycle, order_key
from aiteam.types import PricingPlanAnchor

from .. import _oracle_plan_capacity as oracle_capacity
from .. import _oracle_plan_pricing as oracle_pricing
from .._plan_history import plan_history, price_history

SEEDS = range(24)


def _meta(pricing):
    return None if pricing is None else (pricing.pricing_mode, pricing.catalog_version, pricing.catalog_sha256)


def _anchor_on(snapshot) -> PricingPlanAnchor:
    return PricingPlanAnchor(
        **{field: getattr(snapshot, field) for field in (
            "account_key", "limit_id", "window_duration_ms", "snapshot_id", "observed_at", "used_percent", "resets_at",
        )},
        reset_at=snapshot.observed_at, revision=1,
    )


@pytest.mark.parametrize("seed", SEEDS)
def test_each_step_equals_the_frozen_cycle_fold(seed):
    rows = sorted(price_history(random.Random(seed), 120), key=order_key)
    expected = list(oracle_pricing._cycle_points(rows))
    state, previous, seen = None, None, set()
    for (snapshot, baseline, total, pricing, reason), row in zip(expected, rows, strict=True):
        assert snapshot is row
        step = advance_cycle(state, previous.used_percent if previous else None, row, seen)
        if step.reset:
            seen = set()
        seen.update(request_id for request_id, _ in step.counted)
        state, previous = step.state, row
        assert (state.baseline_id, str(state.total), state.pricing and (
            state.pricing.pricing_mode, state.pricing.catalog_version, state.pricing.catalog_sha256,
        ), state.reason) == (baseline.snapshot_id, str(total), _meta(pricing), reason)
        # the stored prediction (computed by the oracle) is reproduced string for string
        assert str(row.prediction_activity_usd) == str(state.total)
    assert str(fold_cycle(rows)[0].total) == str(oracle_pricing.prediction_cycle_total(rows))


@pytest.mark.parametrize("seed", SEEDS)
def test_seen_intersection_is_enough(seed):
    """Callers pass only the candidate's already-counted IDs, not the whole set."""
    rows = sorted(price_history(random.Random(1000 + seed), 90), key=order_key)
    state, previous, seen = None, None, set()
    for row in rows:
        ids = {item.request_id for quote in (row.pricing.quotes if row.pricing else []) for item in quote.items}
        full = advance_cycle(state, previous.used_percent if previous else None, row, seen)
        narrow = advance_cycle(state, previous.used_percent if previous else None, row, seen & ids)
        assert full == narrow
        if full.reset:
            seen = set()
        seen.update(request_id for request_id, _ in full.counted)
        state, previous = full.state, row


@pytest.mark.parametrize("seed", SEEDS)
def test_price_estimates_equal_the_frozen_estimator(seed):
    rng = random.Random(2000 + seed)
    rows = price_history(rng, 100) + price_history(rng, 30, limit_id="codex_bengalfox")
    codex = sorted((row for row in rows if row.limit_id == "codex"), key=order_key)
    anchors = [[], [_anchor_on(rng.choice(codex))], [_anchor_on(codex[-1])]]
    latest = codex[-1].observed_at
    for anchor_set in anchors:
        for now in (latest, latest + timedelta(seconds=1), latest + timedelta(days=2),
                    latest + timedelta(days=4), codex[0].observed_at - timedelta(days=5)):
            old = [e.model_dump(mode="json") for e in oracle_pricing.estimate_pricing_plan_capacity(
                rows, now=now, anchors=anchor_set)]
            new = [e.model_dump(mode="json") for e in plan_pricing.estimate_pricing_plan_capacity(
                rows, now=now, anchors=anchor_set)]
            assert new == old


@pytest.mark.parametrize("seed", SEEDS)
def test_plan_estimates_equal_the_frozen_estimator(seed):
    rng = random.Random(3000 + seed)
    rows = plan_history(rng, 80) + plan_history(rng, 40, limit_id="codex_bengalfox", window=18_000_000)
    ordered = sorted(rows, key=order_key)
    for cut in sorted(rng.sample(range(1, len(ordered) + 1), 10)) + [len(ordered)]:
        prefix = ordered[:cut]
        for now in (prefix[-1].observed_at + timedelta(seconds=30), prefix[-1].observed_at + timedelta(days=1)):
            old = [e.model_dump(mode="json") for e in oracle_capacity.estimate_plan_capacity(prefix, now=now)]
            new = [e.model_dump(mode="json") for e in plan_capacity.estimate_plan_capacity(prefix, now=now)]
            assert new == old


@pytest.mark.parametrize("seed", SEEDS)
def test_run_start_plus_window_start_equals_the_frozen_baseline(seed):
    """The summary keeps only the run start; the window's first sample covers the rest."""
    rows = sorted(plan_history(random.Random(4000 + seed), 90), key=order_key)
    run_start = None
    for index, row in enumerate(rows):
        run_start = advance_run_start(run_start, rows[index - 1] if index else None, row)
        prefix = rows[: index + 1]
        latest = prefix[-1]
        now = latest.observed_at + timedelta(seconds=5)
        early, window_start = plan_capacity.plan_window_start(latest, now)
        if early is not None:
            continue
        start = next(item for item in prefix if item.snapshot_id == run_start)
        baseline = start if start.observed_at >= window_start else next(
            item for item in prefix if item.observed_at >= window_start
        )
        old = oracle_capacity._estimate_window(prefix, now)
        assert plan_capacity.local_estimate(latest, baseline) == old
