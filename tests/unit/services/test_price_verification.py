"""Price-age metadata never changes the quote or claims whole-account coverage."""

from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from aiteam.services.pricing import load_catalog, price_verification_signals, quote_requests
from aiteam.types import PricingQuoteRequest

NOW = datetime(2026, 10, 15, 12, tzinfo=UTC)


def quote(*models, age=31):
    catalog = load_catalog()
    catalog.aliases["test-astra-snapshot"] = "gpt-6-astra"
    # A recent catalog header or unrelated model must not refresh applied rates.
    catalog.verified_at = NOW
    for record in catalog.records:
        record.verified_at = NOW - timedelta(days=age) if record.model == "gpt-6-astra" else NOW
    request = PricingQuoteRequest(requests=[{
        "request_id": f"r{index}", "model": model, "service_tier": "standard",
        "input_tokens": 1000, "output_tokens": 100,
    } for index, model in enumerate(models)])
    return quote_requests(request, catalog)


@pytest.mark.parametrize(("age", "status"), [(29, "fresh"), (30, "stale"), (31, "stale")])
def test_exact_30_day_boundary_and_per_record_age(age, status):
    result = quote("gpt-6-astra", age=age)
    before = result.model_dump(mode="json")
    signal, = price_verification_signals(result.items, now=NOW)
    assert signal.status == status
    assert signal.verified_at == NOW - timedelta(days=age)
    assert signal.missing_price is False
    assert result.model_dump(mode="json") == before
    assert result.total_usd > 0


def test_just_under_30_days_is_fresh_even_across_timezone_offsets():
    result = quote("gpt-6-astra")
    result.items[0].rate_record.verified_at = (
        NOW - timedelta(days=30) + timedelta(microseconds=1)
    ).astimezone(timezone(timedelta(hours=8)))
    assert price_verification_signals(result.items, now=NOW)[0].status == "fresh"


def test_alias_models_are_resolved_but_only_actual_requests_are_reported():
    alias = "test-astra-snapshot"
    result = quote(alias, "gpt-6-astra", alias, "not-in-catalog")
    signals = {signal.model: signal for signal in price_verification_signals(result.items, now=NOW)}
    assert set(signals) == {alias, "gpt-6-astra", "not-in-catalog"}
    assert signals[alias].canonical_model == "gpt-6-astra"
    assert signals[alias].status == "stale"
    assert signals["not-in-catalog"].status == "unpriced"
    assert signals["not-in-catalog"].missing_price is True
    assert signals["not-in-catalog"].verified_at is None
    assert price_verification_signals([], now=NOW) == []


def test_oldest_actually_applied_rate_wins_and_missing_price_is_independent():
    result = quote("gpt-6-astra", "gpt-6-astra")
    result.items[0].rate_record = result.items[0].rate_record.model_copy(update={"verified_at": NOW})
    result.items[1] = result.items[1].model_copy(update={
        "status": "unpriced", "amount_usd": None, "reason": "cached_input_rate_not_in_catalog",
    })
    signal, = price_verification_signals(result.items, now=NOW)
    assert signal.status == "stale"
    assert signal.missing_price is True


@pytest.mark.parametrize("bad_date", [None, "not-a-date", datetime(2026, 10, 1), NOW + timedelta(seconds=1)],
                         ids=["absent", "invalid", "naive", "future"])
def test_unusable_or_future_verification_never_becomes_fresh(bad_date):
    result = quote("gpt-6-astra")
    # Corrupted metadata is defensive input, not a relaxation of the real schema.
    result.items[0].rate_record = result.items[0].rate_record.model_copy(update={"verified_at": bad_date})
    signal, = price_verification_signals(result.items, now=NOW)
    assert signal.status == "unverified"
    assert signal.verified_at is None
    assert signal.missing_price is False


def test_catalog_still_rejects_missing_or_invalid_verification_metadata():
    data = load_catalog().model_dump(mode="json")
    for invalid in (None, "invalid", "2026-10-01T12:00:00"):
        data["records"][0]["verified_at"] = invalid
        with pytest.raises(ValidationError):
            type(load_catalog()).model_validate(data)


def test_naive_check_time_is_rejected():
    with pytest.raises(ValueError, match="timezone-aware"):
        price_verification_signals([], now=datetime(2026, 10, 15))
