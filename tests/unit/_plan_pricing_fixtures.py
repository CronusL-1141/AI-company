"""Synthetic monetary samples built through production pricing validation."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal, localcontext

from aiteam.services.pricing import _precision, catalog_digest, quote_requests
from aiteam.types import (
    PricingAccount,
    PricingCatalog,
    PricingPlanSample,
    PricingPlanSnapshot,
    PricingQuotaSnapshot,
    PricingQuoteRequest,
    PricingRequestLine,
    PricingUsageEntry,
)

BASE = datetime(2026, 9, 15, 0, tzinfo=UTC)
KEY = "a" * 64
SCOPE = "b" * 64


def catalog(version="fixture-v1"):
    records = []
    for model, minimum, maximum, rates in [
        ("model-a", 0, 100000, ("2", ".2", "3", "10")),
        ("model-a", 100001, None, ("4", ".4", "6", "15")),
        ("model-b", 0, None, ("10", "1", "15", "20")),
    ]:
        records.append({
            "model": model, "tier": "standard", "min_input_tokens": minimum, "max_input_tokens": maximum,
            "rates": dict(zip(("input", "cached_input", "cache_write", "output"), rates, strict=True)),
            "source_url": "https://developers.openai.com/api/docs/pricing", "verified_at": BASE,
            "effective_from": None, "effective_until": None, "notes": "Synthetic test rates.",
        })
    return PricingCatalog.model_validate({
        "schema_version": 1, "version": version, "currency": "USD", "verified_at": BASE,
        "records": records, "aliases": {"model-alias": "model-a"}, "unpriced_models": [],
    })


def entry(identifier="response-a", *, at=BASE + timedelta(seconds=1), model="model-a", **changes):
    return PricingUsageEntry(occurred_at=at, request=PricingRequestLine.model_validate({
        "request_id": identifier, "model": model, "service_tier": "standard",
        "input_tokens": 1000, "cached_input_tokens": 200,
        "cache_write_input_tokens": 300, "output_tokens": 100, **changes,
    }))


def sample(*, start=BASE, end=BASE, entries=(), price_catalog=None, complete=None):
    price_catalog = price_catalog or catalog()
    entries = list(entries)
    quotes = [
        quote_requests(PricingQuoteRequest(requests=[item.request for item in entries[i:i + 1000]]), price_catalog)
        for i in range(0, len(entries), 1000)
    ]
    return PricingPlanSample(
        pricing_mode="standard_equivalent", catalog_version=price_catalog.version,
        catalog_sha256=catalog_digest(price_catalog), interval_start=start, interval_end=end,
        entries=entries, quotes=quotes,
        complete=all(quote.complete for quote in quotes) if complete is None else complete,
    )


def price_snapshot(
    identifier="baseline", *, at=BASE, percent=20, previous_usd=Decimal(0),
    start=BASE, binding=BASE, scope=SCOPE, key=KEY, limit_id="codex", pricing=None,
    entries=(), price_catalog=None, complete=None, unavailable=False,
):
    pricing = pricing or sample(start=start, end=at, entries=entries, price_catalog=price_catalog, complete=complete)
    if pricing.complete:
        values = [previous_usd, *(quote.total_usd for quote in pricing.quotes)]
        with localcontext() as context:
            context.prec = _precision(values)
            value = sum(values, Decimal(0))
    else:
        value = None
    return PricingPlanSnapshot(
        snapshot_id=identifier, account_key=key, limit_id=limit_id, window_duration_ms=604800000,
        resets_at=BASE + timedelta(days=3), observed_at=at, used_percent=percent,
        activity_scope=None if unavailable else scope, activity_binding_at=None if unavailable else binding,
        activity_usd=None if unavailable else value, pricing=None if unavailable else pricing,
    )


def quota(snapshot):
    return PricingQuotaSnapshot(
        **{field: getattr(snapshot, field) for field in (
            "snapshot_id", "account_key", "limit_id", "window_duration_ms", "resets_at", "observed_at", "used_percent",
        )},
        source="codex_app_server",
    )


def account(key=KEY):
    return PricingAccount(account_key=key, label="Local fixture account", created_at=BASE)
