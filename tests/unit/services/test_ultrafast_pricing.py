"""Ultrafast API prices stay separate from subscription speed rules."""

import hashlib
import json
from decimal import Decimal

import pytest
from pydantic import ValidationError

from aiteam.services.pricing import catalog_digest, load_catalog, quote_requests
from aiteam.types import PricingCatalog, PricingQuoteItem, PricingQuoteRequest, PricingRequestLine


def line(model="gpt-6.1-sol", tier="ultrafast", **changes):
    return PricingRequestLine(request_id="one", model=model, service_tier=tier,
                              input_tokens=changes.pop("input_tokens", 272000),
                              cached_input_tokens=10000, cache_write_input_tokens=20000,
                              output_tokens=1000, **changes)


@pytest.mark.parametrize(("model", "tokens", "expected"), [
    ("gpt-6-astra", 272000, "16.38"),
    ("gpt-6-astra", 272001, "32.61012"),
    ("gpt-6.1-sol", 272000, "3.27"),
    ("gpt-6.1-sol", 272001, "6.510024"),
])
def test_official_ultrafast_context_and_cache_rates(model, tokens, expected):
    result = quote_requests(PricingQuoteRequest(requests=[line(model, input_tokens=tokens)]), load_catalog())
    assert result.complete and result.total_usd == Decimal(expected)
    record = result.items[0].rate_record
    assert record.tier == "ultrafast"
    assert record.effective_from is record.effective_until is None
    assert record.min_input_tokens == (0 if tokens == 272000 else 272001)


@pytest.mark.parametrize(("model", "tier", "tokens", "reason"), [
    ("gpt-6.1-sol", "ultrafast", 922001, "input_interval_not_in_catalog"),
    ("gpt-5.6-sol", "ultrafast", 272000, "service_tier_not_in_catalog"),
    ("gpt-6.1-sol", "future-tier", 272000, "service_tier_not_in_catalog"),
])
def test_uncovered_combinations_remain_unpriced(model, tier, tokens, reason):
    result = quote_requests(PricingQuoteRequest(requests=[line(model, tier, input_tokens=tokens)]), load_catalog())
    assert not result.complete and result.total_usd is None
    assert result.items[0].service_tier == tier and result.items[0].reason == reason


@pytest.mark.parametrize("tier", ["future-tier", "x\ny", "\nx"])
def test_nonblank_unknown_tier_is_preserved(tier):
    assert line(tier=tier).service_tier == tier


@pytest.mark.parametrize("tier", ["", " ", "\n\t"])
def test_blank_tier_rejected(tier):
    with pytest.raises(ValidationError):
        line(tier=tier)


def test_tier_length_is_bounded_on_request_and_quote():
    longest = "x" * 256
    item = quote_requests(PricingQuoteRequest(requests=[line(tier=longest)]), load_catalog()).items[0]
    assert item.service_tier == longest and item.reason == "service_tier_not_in_catalog"
    with pytest.raises(ValidationError):
        line(tier=longest + "x")
    wire = item.model_dump(mode="json")
    wire["service_tier"] = longest + "x"
    with pytest.raises(ValidationError):
        PricingQuoteItem.model_validate(wire)


def test_subscription_metadata_never_changes_api_quote_and_old_catalog_identity():
    catalog = load_catalog()
    rules = {mode.service_tier: mode for mode in catalog.speed_modes}
    assert (rules["fast"].included_usage_multiplier, rules["fast"].purchased_credits_multiplier) == (
        Decimal("2.5"), Decimal("2"))
    assert (rules["ultrafast"].included_usage_multiplier, rules["ultrafast"].purchased_credits_multiplier) == (
        Decimal("8"), Decimal("6"))
    assert rules["ultrafast"].models == ["gpt-6-astra", "gpt-6.1-sol"]
    requests = PricingQuoteRequest(requests=[line()])
    wire = catalog.model_dump(mode="json")
    wire.pop("speed_modes")
    old = PricingCatalog.model_validate(wire)
    assert quote_requests(requests, catalog).total_usd == quote_requests(requests, old).total_usd
    canonical = json.dumps(wire, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    assert catalog_digest(old) == hashlib.sha256(canonical.encode()).hexdigest()
    assert catalog_digest(old) != catalog_digest(catalog)


def test_speed_rules_reject_duplicates_and_invalid_multipliers():
    wire = load_catalog().model_dump(mode="json")
    wire["speed_modes"].append(wire["speed_modes"][0])
    with pytest.raises(ValidationError, match="unique service tiers"):
        PricingCatalog.model_validate(wire)
    for bad in ("0", "-1", "NaN", 2.5):
        wire = load_catalog().model_dump(mode="json")
        wire["speed_modes"][0]["included_usage_multiplier"] = bad
        with pytest.raises(ValidationError):
            PricingCatalog.model_validate(wire)
    wire = load_catalog().model_dump(mode="json")
    wire["speed_modes"][0]["models"].append(wire["speed_modes"][0]["models"][0])
    with pytest.raises(ValidationError, match="unique"):
        PricingCatalog.model_validate(wire)
