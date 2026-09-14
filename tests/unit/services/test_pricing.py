"""Pure pricing coverage and validation, without storage or account access."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from decimal import Decimal, localcontext
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from aiteam.services.pricing import catalog_digest, decode_pricing_json, load_catalog, quote_requests
from aiteam.types import PricingCatalog, PricingQuoteItem, PricingQuoteRequest, PricingQuoteResponse, PricingRequestLine

SOURCE = "https://developers.openai.com/api/docs/pricing"
VERIFIED = "2026-09-14T04:00:00Z"


def _record(**overrides: Any) -> dict[str, Any]:
    return {
        "model": "example-model", "tier": "standard", "min_input_tokens": 0,
        "max_input_tokens": 272000,
        "rates": {"input": "2", "cached_input": "0.2", "cache_write": "3", "output": "10"},
        "source_url": SOURCE, "verified_at": VERIFIED,
        "effective_from": None, "effective_until": None, "notes": "Synthetic test rates.",
        **overrides,
    }


def _catalog_data() -> dict[str, Any]:
    return {
        "schema_version": 1, "version": "test-v1", "currency": "USD", "verified_at": VERIFIED,
        "records": [
            _record(),
            _record(min_input_tokens=272001, max_input_tokens=None, rates={
                "input": "4", "cached_input": "0.4", "cache_write": "6", "output": "15",
            }),
            _record(tier="fast", max_input_tokens=None, rates={
                "input": "4", "cached_input": "0.4", "cache_write": "6", "output": "20",
            }),
        ],
        "aliases": {"example-snapshot": "example-model"},
        "unpriced_models": [{"model": "unpublished", "reason": "No public rate.", "source_url": SOURCE}],
    }


def _line(**overrides: Any) -> dict[str, Any]:
    return {
        "request_id": "r1", "model": "example-model", "service_tier": "standard",
        "input_tokens": 1000, "output_tokens": 100, **overrides,
    }


def _quote(*lines: dict[str, Any], data: dict[str, Any] | None = None):
    return quote_requests(
        PricingQuoteRequest(requests=list(lines)),
        PricingCatalog.model_validate(data if data is not None else _catalog_data()),
    )


@pytest.mark.parametrize(("tokens", "expected", "minimum"), [
    (272000, "0.545", 0),
    (272001, "1.089504", 272001),
])
def test_inclusive_context_boundary_uses_total_input(tokens, expected, minimum):
    result = _quote(_line(input_tokens=tokens))
    assert result.total_usd == Decimal(expected)
    assert result.items[0].rate_record.min_input_tokens == minimum
    assert result.items[0].rate_record.verified_at == result.verified_at


def test_requests_select_context_individually_and_cache_does_not_reduce_context():
    result = _quote(_line(input_tokens=200000), _line(request_id="r2", input_tokens=200000))
    assert result.total_usd == Decimal("0.802")
    assert all(item.rate_record.min_input_tokens == 0 for item in result.items)
    cached = _quote(_line(input_tokens=272001, cached_input_tokens=272001, output_tokens=0))
    assert cached.total_usd == Decimal("0.1088004")
    assert cached.items[0].rate_record.min_input_tokens == 272001


def test_input_is_partitioned_once_into_ordinary_cached_and_write():
    result = _quote(_line(cached_input_tokens=200, cache_write_input_tokens=300))
    assert result.total_usd == Decimal("0.00294")
    assert result.priced_subtotal_usd == result.items[0].amount_usd
    assert result.model_dump(mode="json")["total_usd"] == "0.00294"
    assert result.complete and result.missing_models == []
    assert result.basis == "api_equivalent_at_catalog_version"


@pytest.mark.parametrize(("tier", "expected", "canonical_tier"), [
    ("standard", "0.003", "standard"), ("default", "0.003", "standard"),
    ("fast", "0.006", "fast"), ("priority", "0.006", "fast"),
])
def test_explicit_tier_and_exact_model_alias(tier, expected, canonical_tier):
    result = _quote(_line(model="example-snapshot", service_tier=tier))
    assert result.total_usd == Decimal(expected)
    assert result.items[0].canonical_model == "example-model"
    assert result.items[0].service_tier == tier
    assert result.items[0].rate_record.tier == canonical_tier


@pytest.mark.parametrize(("overrides", "reason"), [
    ({"model": "example-model-new"}, "model_not_in_catalog"),
    ({"model": "unpublished"}, "No public rate."),
    ({"service_tier": "flex"}, "service_tier_not_in_catalog"),
    ({"service_tier": "batch"}, "service_tier_not_in_catalog"),
])
def test_missing_model_or_tier_is_not_guessed(overrides, reason):
    result = _quote(_line(**overrides))
    assert result.total_usd is None
    assert result.priced_subtotal_usd == Decimal(0)
    assert result.items[0].reason == reason
    assert result.items[0].rate_record is None
    assert not result.complete


def test_uncovered_context_does_not_fall_back():
    data = _catalog_data()
    data["records"] = [data["records"][0]]
    result = _quote(_line(input_tokens=272001), data=data)
    assert result.items[0].reason == "input_interval_not_in_catalog"
    assert result.total_usd is None


@pytest.mark.parametrize(("rate", "counter", "reason"), [
    ("cached_input", "cached_input_tokens", "cached_input_rate_not_in_catalog"),
    ("cache_write", "cache_write_input_tokens", "cache_write_rate_not_in_catalog"),
])
def test_null_cache_rate_is_only_needed_for_observed_usage(rate, counter, reason):
    data = _catalog_data()
    data["records"][0]["rates"][rate] = None
    missing = _quote(_line(**{counter: 1}), data=data)
    assert missing.items[0].reason == reason
    assert missing.items[0].rate_record is not None
    assert missing.total_usd is None
    unused = _quote(_line(), data=data)
    assert unused.total_usd == Decimal("0.003")


def test_partial_subtotal_and_same_inputs_recalculated_after_supplement():
    lines = [_line(), _line(request_id="r2", model="unpublished")]
    original = _catalog_data()
    partial = _quote(*lines, data=original)
    assert (partial.request_count, partial.priced_request_count, partial.unpriced_request_count) == (2, 1, 1)
    assert partial.total_usd is None
    assert partial.priced_subtotal_usd == Decimal("0.003")
    assert partial.missing_models == ["unpublished"]
    supplemented = deepcopy(original)
    supplemented["version"] = "test-v2"
    supplemented["records"].append(_record(model="unpublished"))
    supplemented["unpriced_models"] = []
    complete = _quote(*lines, data=supplemented)
    assert complete.complete
    assert complete.total_usd == Decimal("0.006")
    assert complete.catalog_version != partial.catalog_version
    assert complete.catalog_sha256 != partial.catalog_sha256
    assert complete.items[0] == partial.items[0]


def test_no_rounding_from_ambient_decimal_context_and_zero_is_priced():
    data = _catalog_data()
    data["records"][0]["rates"]["input"] = "0.123456789012345678901234567890123456789"
    with localcontext() as context:
        context.prec = 6
        result = _quote(_line(input_tokens=1, output_tokens=0), data=data)
    assert result.total_usd == Decimal("0.000000123456789012345678901234567890123456789")
    empty = _quote(_line(input_tokens=0, output_tokens=0))
    assert empty.complete and empty.items[0].status == "priced"
    assert empty.total_usd == Decimal(0)


@pytest.mark.parametrize("field", [
    "input_tokens", "output_tokens", "cached_input_tokens", "cache_write_input_tokens",
])
@pytest.mark.parametrize("invalid", [True, False, -1, 1.0, 1.5, "1"])
def test_token_counts_are_strict_nonnegative_integers(field, invalid):
    with pytest.raises(ValidationError):
        PricingRequestLine.model_validate(_line(**{field: invalid}))


@pytest.mark.parametrize("field", ["request_id", "model", "service_tier", "input_tokens", "output_tokens"])
def test_required_request_fields(field):
    line = _line()
    del line[field]
    with pytest.raises(ValidationError):
        PricingRequestLine.model_validate(line)


def test_request_validation_cache_sum_duplicates_bounds_and_reasoning():
    with pytest.raises(ValidationError):
        PricingRequestLine.model_validate(_line(cached_input_tokens=600, cache_write_input_tokens=401))
    with pytest.raises(ValidationError):
        PricingRequestLine.model_validate(_line(reasoning_tokens=10))
    with pytest.raises(ValidationError):
        PricingQuoteRequest(requests=[_line(), _line()])
    with pytest.raises(ValidationError):
        PricingQuoteRequest(requests=[])
    with pytest.raises(ValidationError):
        PricingQuoteRequest(requests=[_line(request_id=str(i)) for i in range(1001)])
    assert len(PricingQuoteRequest(requests=[_line(request_id=str(i)) for i in range(1000)]).requests) == 1000
    assert PricingRequestLine.model_validate(_line()).cached_input_tokens == 0
    assert PricingRequestLine.model_validate(_line()).cache_write_input_tokens == 0


@pytest.mark.parametrize("invalid", ["-1", "NaN", "Infinity", "-Infinity", 1, 1.0, True])
@pytest.mark.parametrize("field", ["input", "cached_input", "cache_write", "output"])
def test_catalog_rejects_bad_rates(field, invalid):
    data = _catalog_data()
    data["records"][0]["rates"][field] = invalid
    with pytest.raises(ValidationError):
        PricingCatalog.model_validate(data)


@pytest.mark.parametrize("source", [
    "http://openai.com/api/pricing", "https://openai.com.attacker.test/pricing",
    "https://attacker.test/openai.com", "https://user@openai.com/pricing",
    "https://openai.com:444/pricing", "https://subdomain.openai.com/pricing",
])
def test_catalog_rejects_untrusted_sources(source):
    data = _catalog_data()
    data["records"][0]["source_url"] = source
    with pytest.raises(ValidationError):
        PricingCatalog.model_validate(data)


@pytest.mark.parametrize("mutate", [
    lambda data: data.update(schema_version=True),
    lambda data: data.update(currency="EUR"),
    lambda data: data.update(extra=True),
    lambda data: data.update(verified_at="2026-09-14T04:00:00"),
    lambda data: data["records"][0].update(verified_at="2026-09-14T04:00:00"),
    lambda data: data["records"][0].update(effective_from="2026-09-14T04:00:00"),
    lambda data: data["records"][0].update(
        effective_from="2026-09-15T00:00:00Z", effective_until="2026-09-14T00:00:00Z",
    ),
    lambda data: data["records"][0].pop("verified_at"),
    lambda data: data["records"][0]["rates"].pop("cache_write"),
    lambda data: data["records"][0].update(max_input_tokens=-1),
    lambda data: data["records"][0].update(min_input_tokens=True),
    lambda data: data["records"][0].update(max_input_tokens=1.0),
    lambda data: data["records"][0].update(min_input_tokens=10, max_input_tokens=9),
    lambda data: data["records"][1].update(min_input_tokens=272000),
    lambda data: data["records"][0].update(max_input_tokens=None),
    lambda data: data["records"].append(deepcopy(data["records"][0])),
    lambda data: data["aliases"].update({"example-model": "example-snapshot"}),
    lambda data: data["aliases"].update({"a": "b", "b": "a"}),
    lambda data: data["aliases"].update({"broken": "absent"}),
    lambda data: data["unpriced_models"].append(deepcopy(data["unpriced_models"][0])),
    lambda data: data["unpriced_models"][0].update(model="example-model"),
    lambda data: data["unpriced_models"][0].update(source_url="https://attacker.test"),
])
def test_catalog_rejects_structural_corruption(mutate):
    data = _catalog_data()
    mutate(data)
    with pytest.raises(ValidationError):
        PricingCatalog.model_validate(data)


def test_verification_timestamp_is_not_an_effective_date():
    data = _catalog_data()
    data["records"][0]["effective_from"] = "2020-01-01T00:00:00Z"
    data["records"][0]["effective_until"] = "2020-12-31T23:59:59Z"
    result = _quote(_line(), data=data)
    assert result.total_usd == Decimal("0.003")
    assert result.items[0].rate_record.effective_until.year == 2020
    assert result.verified_at.year == 2026


def test_catalog_digest_is_canonical_and_covers_metadata():
    data = _catalog_data()
    catalog = PricingCatalog.model_validate(data)
    canonical = json.dumps(catalog.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    assert catalog_digest(catalog) == hashlib.sha256(canonical.encode()).hexdigest()
    assert catalog_digest(PricingCatalog.model_validate(dict(reversed(list(data.items()))))) == catalog_digest(catalog)
    data["records"][0]["verified_at"] = "2026-09-14T05:00:00Z"
    assert catalog_digest(PricingCatalog.model_validate(data)) != catalog_digest(catalog)


def test_load_explicit_environment_and_packaged_catalog(tmp_path: Path, monkeypatch):
    path = tmp_path / "catalog.json"
    content = json.dumps(_catalog_data())
    path.write_text(content, encoding="utf-8")
    assert load_catalog(path).version == "test-v1"
    monkeypatch.setenv("AITEAM_PRICING_CATALOG", str(path))
    assert load_catalog().version == "test-v1"
    monkeypatch.setenv("AITEAM_PRICING_CATALOG", str(tmp_path / "missing.json"))
    assert load_catalog(path).version == "test-v1"
    with pytest.raises(FileNotFoundError):
        load_catalog()
    monkeypatch.delenv("AITEAM_PRICING_CATALOG")
    packaged = load_catalog()
    assert packaged.currency == "USD" and packaged.records
    assert len(catalog_digest(packaged)) == 64
    assert path.read_text(encoding="utf-8") == content


@pytest.mark.parametrize("text", ["{", '{"version":"one","version":"two"}', "[]"])
def test_load_rejects_damaged_catalog(tmp_path: Path, text):
    path = tmp_path / "bad.json"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError):
        load_catalog(path)


@pytest.mark.parametrize("overrides", [
    {"request_count": 10}, {"priced_request_count": 0}, {"unpriced_request_count": 1},
    {"complete": False}, {"total_usd": None}, {"priced_subtotal_usd": "1", "total_usd": "1"},
])
def test_quote_response_rejects_inconsistent_totals(overrides):
    data = _quote(_line()).model_dump()
    data.update(overrides)
    with pytest.raises(ValidationError):
        PricingQuoteResponse.model_validate(data)


def test_partial_response_and_item_cannot_claim_a_total():
    data = _quote(_line(), _line(request_id="r2", model="unpublished")).model_dump()
    data["total_usd"] = data["priced_subtotal_usd"]
    with pytest.raises(ValidationError):
        PricingQuoteResponse.model_validate(data)
    item = data["items"][1]
    item["amount_usd"] = Decimal("0")
    with pytest.raises(ValidationError):
        PricingQuoteItem.model_validate(item)


@pytest.mark.parametrize("content", [
    '{"requests":[],"requests":[{}]}',
    b'{"requests":[{"input_tokens":1,"input_tokens":2}]}',
])
def test_public_json_decoder_rejects_duplicate_request_keys(content):
    with pytest.raises(ValueError, match="duplicate"):
        decode_pricing_json(content)
