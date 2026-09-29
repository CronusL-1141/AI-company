"""Random plan and price histories built through production validation.

Shaped like the monitor's output: one observation per capture, adjacent pricing
intervals, occasional quota resets, unavailable and incomplete samples, Spark and
unpriced requests, assumed service tiers, repeated request IDs, and local token
runs that break on scope, binding, rollback or a quota reset. Predictions and
cumulative dollars are what the frozen a4bab96 code would have stored.
"""

from __future__ import annotations

import random
from datetime import timedelta
from decimal import Decimal, localcontext

from aiteam.services.pricing import _precision
from aiteam.types import (
    PlanUsageSnapshot,
    PricingCatalog,
    PricingPlanSnapshot,
    PricingRequestLine,
    PricingUsageEntry,
)

from . import _oracle_plan_pricing as oracle_pricing
from ._plan_pricing_fixtures import BASE, KEY, SCOPE, sample

WEEK = 604_800_000
SPARK = "gpt-5.3-codex-spark"


def history_catalog() -> PricingCatalog:
    records = []
    for model, minimum, maximum, rates in [
        ("model-a", 0, 100000, ("2", ".2", "3", "10")),
        ("model-a", 100001, None, ("4", ".4", "6", "15")),
        ("model-b", 0, None, ("10", "1", "15", "20")),
        (SPARK, 0, None, ("1", ".1", "1.5", "4")),
    ]:
        records.append({
            "model": model, "tier": "standard", "min_input_tokens": minimum, "max_input_tokens": maximum,
            "rates": dict(zip(("input", "cached_input", "cache_write", "output"), rates, strict=True)),
            "source_url": "https://developers.openai.com/api/docs/pricing", "verified_at": BASE,
            "effective_from": None, "effective_until": None, "notes": "Synthetic test rates.",
        })
    return PricingCatalog.model_validate({
        "schema_version": 1, "version": "history-v1", "currency": "USD", "verified_at": BASE,
        "records": records, "aliases": {}, "unpriced_models": [],
    })


def _entry(rng: random.Random, request_id: str, start, end, *, allow_assumed: bool) -> PricingUsageEntry:
    span = (end - start).total_seconds()
    at = start + timedelta(seconds=max(0.001, rng.uniform(0.001, span)))
    model = rng.choices(["model-a", "model-b", SPARK, "model-x"], [62, 22, 13, 3])[0]
    source = rng.choice([None, "payload", "standard_assumption"] if allow_assumed else [None, "payload"])
    return PricingUsageEntry(
        occurred_at=min(at, end), service_tier_source=source,
        request=PricingRequestLine.model_validate({
            "request_id": request_id, "model": model, "service_tier": "standard",
            "input_tokens": rng.choice([1000, 5000, 150000]), "cached_input_tokens": 200,
            "cache_write_input_tokens": rng.choice([0, 300]), "output_tokens": rng.randint(1, 900),
        }),
    )


def price_history(
    rng: random.Random, count: int, *, key: str = KEY, limit_id: str = "codex",
    window: int = WEEK, step_seconds: int = 30,
) -> list[PricingPlanSnapshot]:
    """One window's snapshots in capture order, with the predictions a4bab96 stored."""
    catalog = history_catalog()
    rows: list[PricingPlanSnapshot] = []
    used_ids: list[str] = []
    percent = rng.randint(5, 30)
    previous_at = None
    previous_usd: Decimal | None = None
    binding = None  # the local source binding the dollar chain started at
    for index in range(count):
        at = BASE + timedelta(seconds=step_seconds * (index + 1) + rng.random())
        if rng.random() < 0.06:
            percent = rng.randint(0, max(0, percent - 1))  # quota reset
        elif rng.random() < 0.5:
            percent = min(100, percent + 1)
        snapshot_id = f"{limit_id}-{index:05d}-{rng.randrange(16**6):06x}"
        common = dict(snapshot_id=snapshot_id, account_key=key, limit_id=limit_id, window_duration_ms=window,
                      resets_at=BASE + timedelta(days=3), observed_at=at, used_percent=percent)
        roll = rng.random()
        if limit_id != "codex" or roll < 0.08:
            snapshot = PricingPlanSnapshot(**common)  # pricing unavailable this round
            previous_usd = None
        elif previous_at is None or binding is None:
            # first bound observation: empty interval at the binding point
            binding = at
            snapshot = PricingPlanSnapshot(
                **common, activity_scope=SCOPE, activity_binding_at=at, activity_usd=Decimal(0),
                pricing=sample(start=at, end=at, entries=[], price_catalog=catalog),
            )
            previous_usd = Decimal(0)
        else:
            incomplete = roll < 0.12
            entries = []
            for _ in range(rng.choice([0, 1, 1, 2, 3])):
                if used_ids and rng.random() < 0.08:
                    request_id = rng.choice(used_ids)  # the same response seen again later
                    if any(item.request.request_id == request_id for item in entries):
                        continue
                else:
                    request_id = f"resp-{len(used_ids):06d}"
                    used_ids.append(request_id)
                entries.append(_entry(rng, request_id, previous_at, at, allow_assumed=incomplete))
            pricing = sample(start=previous_at, end=at, entries=entries, price_catalog=catalog,
                             complete=False if incomplete else None)
            amount = None
            if pricing.complete and previous_usd is not None:
                values = [previous_usd, *(quote.total_usd for quote in pricing.quotes)]
                with localcontext() as context:
                    context.prec = _precision(values)
                    amount = sum(values, Decimal(0))
            snapshot = PricingPlanSnapshot(
                **common, activity_scope=SCOPE, activity_binding_at=binding, activity_usd=amount, pricing=pricing,
            )
            previous_usd = amount
        if limit_id == "codex":
            snapshot = snapshot.model_copy(update={
                "prediction_activity_usd": oracle_pricing.prediction_cycle_total([*rows, snapshot]),
            })
        rows.append(PricingPlanSnapshot.model_validate(snapshot.model_dump(mode="python")))
        previous_at = at
    return rows


def plan_history(
    rng: random.Random, count: int, *, key: str = KEY, limit_id: str = "codex",
    window: int = 3_600_000, step_seconds: int = 300,
) -> list[PlanUsageSnapshot]:
    """Local token samples whose run breaks in every way the estimator recognises."""
    rows: list[PlanUsageSnapshot] = []
    tokens = rng.randint(0, 10_000)
    percent = rng.randint(0, 20)
    scope, binding = SCOPE, BASE
    resets_at = BASE + timedelta(milliseconds=window * 0.7)
    for index in range(count):
        at = BASE + timedelta(seconds=step_seconds * (index + 1))
        while resets_at <= at:  # the quota window rolled over
            resets_at += timedelta(milliseconds=window)
            if rng.random() < 0.8:
                percent = rng.randint(0, 3)
        if rng.random() < 0.05:
            scope = f"{rng.randrange(16**64):064x}"
        if rng.random() < 0.05:
            binding = at - timedelta(seconds=rng.randint(0, step_seconds))
        if rng.random() < 0.05:
            tokens = max(0, tokens - rng.randint(1, 500))  # counter rollback
        else:
            tokens += rng.randint(0, 5000)
        if rng.random() < 0.05:
            percent = rng.randint(0, max(0, percent - 1))
        elif rng.random() < 0.4:
            percent = min(100, percent + 1)
        if rng.random() < 0.3:
            resets_at += timedelta(seconds=rng.choice([1, -1, window // 30000]))  # provider jitter
        local = rng.random() > 0.06
        rows.append(PlanUsageSnapshot(
            snapshot_id=f"plan-{limit_id}-{index:05d}", account_key=key, limit_id=limit_id,
            window_duration_ms=window, resets_at=resets_at, observed_at=at, used_percent=percent,
            source="codex_local_logs",
            activity_observed_at=at if local else None, activity_tokens=tokens if local else None,
            activity_scope=scope if local else None,
            activity_binding_at=min(binding, at) if local else None,
        ))
    return rows
