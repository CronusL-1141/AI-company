"""Account sample estimates stay within explicit account and coverage bounds."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal, localcontext
from typing import Any

import pytest
from pydantic import ValidationError

from aiteam.services.account_usage import estimate_batch, validate_batch_window
from aiteam.types import (
    PricingAccount,
    PricingAccountEstimate,
    PricingCatalog,
    PricingQuotaSnapshot,
    PricingUsageBatch,
)

ACCOUNT = "a" * 64
START = datetime(2026, 9, 14, 4, 0, tzinfo=UTC)
END = START + timedelta(hours=1)
RESET = START + timedelta(days=3)


def _snapshot(**changes: Any) -> PricingQuotaSnapshot:
    return PricingQuotaSnapshot.model_validate({
        "snapshot_id": "start", "account_key": ACCOUNT, "limit_id": "codex",
        "used_percent": "10", "window_duration_ms": 604800000,
        "resets_at": RESET, "observed_at": START, "source": "codex_app_server", **changes,
    })


def _end(**changes: Any) -> PricingQuotaSnapshot:
    return _snapshot(**{"snapshot_id": "end", "used_percent": "20", "observed_at": END, **changes})


def _entry(**changes: Any) -> dict[str, Any]:
    return {
        "occurred_at": END,
        "request": {
            "request_id": "r1", "model": "example-model", "service_tier": "standard",
            "input_tokens": 10000, "output_tokens": 1000,
        }, **changes,
    }


def _batch(**changes: Any) -> PricingUsageBatch:
    return PricingUsageBatch.model_validate({
        "batch_id": "batch", "account_key": ACCOUNT, "start_snapshot_id": "start", "end_snapshot_id": "end",
        "coverage": "account_complete", "coverage_statement": "本人确认包含同期所有客户端的完整用量。",
        "coverage_confirmed_at": END + timedelta(minutes=1), "entries": [_entry()], **changes,
    })


def _catalog() -> PricingCatalog:
    return PricingCatalog.model_validate({
        "schema_version": 1, "version": "test-v1", "currency": "USD", "verified_at": START,
        "records": [{
            "model": "example-model", "tier": "standard", "min_input_tokens": 0, "max_input_tokens": None,
            "rates": {"input": "2", "cached_input": "0.2", "cache_write": None, "output": "10"},
            "source_url": "https://developers.openai.com/api/docs/pricing", "verified_at": START,
            "effective_from": None, "effective_until": None, "notes": "Synthetic test rates.",
        }],
        "aliases": {}, "unpriced_models": [],
    })


def test_complete_confirmed_sample_supports_conditional_week_estimate():
    result = estimate_batch(_batch(), _snapshot(), _end(), _catalog())
    assert result.status == "conditional"
    assert result.quote.total_usd == Decimal("0.03")
    assert result.delta_used_percent == Decimal("10")
    assert result.estimated_full_week_usd == Decimal("0.3")
    assert (result.interval_start, result.interval_end) == (START, END)
    assert result.coverage_confirmed_at == END + timedelta(minutes=1)
    assert result.account_key == ACCOUNT and result.batch_id == "batch"
    assert "所有客户端" in result.reason and "模型、服务档位和用量组合" in result.reason
    assert "不是官方额度价值或实际账单" in result.reason
    assert "未经系统独立验证" in result.reason
    serialized_amount = result.model_dump(mode="json")["estimated_full_week_usd"]
    assert isinstance(serialized_amount, str)
    assert Decimal(serialized_amount) == Decimal("0.3")


def test_local_only_never_extrapolates_even_with_complete_prices():
    result = estimate_batch(
        _batch(coverage="local_only", coverage_statement="", coverage_confirmed_at=None),
        _snapshot(), _end(), _catalog(),
    )
    assert result.status == "sample_only"
    assert result.estimated_full_week_usd is None
    assert result.quote.total_usd == Decimal("0.03")


@pytest.mark.parametrize("used", ["0", "9", "10", "10.5", "11"])
def test_nonpositive_or_quantized_delta_preserves_sample_without_week_amount(used):
    result = estimate_batch(_batch(), _snapshot(), _end(used_percent=used), _catalog())
    assert result.status == "unavailable"
    assert result.estimated_full_week_usd is None
    assert result.quote.total_usd == Decimal("0.03")
    if Decimal(used) > 10:
        assert "1 个百分点" in result.reason and "量化" in result.reason


def test_just_over_one_point_is_conditional_with_explicit_assumptions():
    result = estimate_batch(_batch(), _snapshot(), _end(used_percent="11.01"), _catalog())
    assert result.status == "conditional"
    assert result.delta_used_percent == Decimal("1.01")


def test_missing_price_prevents_week_estimate_but_retains_priced_subtotal():
    unknown = _entry(request={
        "request_id": "r2", "model": "unknown", "service_tier": "standard",
        "input_tokens": 1, "output_tokens": 1,
    })
    result = estimate_batch(_batch(entries=[_entry(), unknown]), _snapshot(), _end(), _catalog())
    assert result.status == "unavailable" and result.estimated_full_week_usd is None
    assert result.quote.total_usd is None
    assert result.quote.priced_subtotal_usd == Decimal("0.03")
    assert result.quote.missing_models == ["unknown"]


@pytest.mark.parametrize(("start_changes", "end_changes", "batch_changes", "message"), [
    ({}, {}, {"start_snapshot_id": "other"}, "snapshot IDs"),
    ({}, {}, {"end_snapshot_id": "other"}, "snapshot IDs"),
    ({}, {"account_key": "b" * 64}, {}, "same account"),
    ({}, {}, {"account_key": "b" * 64}, "same account"),
    ({}, {"limit_id": "other"}, {}, "same quota bucket"),
    ({"window_duration_ms": 18000000}, {}, {}, "weekly window"),
    ({}, {"window_duration_ms": 18000000}, {}, "weekly window"),
    ({}, {"resets_at": RESET + timedelta(days=7)}, {}, "quota reset"),
    ({}, {"observed_at": START}, {}, "strictly increasing"),
    ({}, {"observed_at": START - timedelta(seconds=1)}, {}, "strictly increasing"),
    ({"observed_at": RESET - timedelta(days=8)}, {}, {}, "reset window"),
    ({}, {"observed_at": RESET}, {}, "reset window"),
])
def test_invalid_account_bucket_or_window_is_rejected(start_changes, end_changes, batch_changes, message):
    with pytest.raises(ValueError, match=message):
        validate_batch_window(_batch(**batch_changes), _snapshot(**start_changes), _end(**end_changes))


@pytest.mark.parametrize("occurred_at", [START - timedelta(days=7), START, END + timedelta(microseconds=1)])
def test_old_or_outside_requests_cannot_be_paired_with_new_quota(occurred_at):
    with pytest.raises(ValueError, match="request timestamps"):
        estimate_batch(_batch(entries=[_entry(occurred_at=occurred_at)]), _snapshot(), _end(), _catalog())


def test_half_open_interval_accepts_inside_and_right_boundary():
    inside = _entry(occurred_at=START + timedelta(microseconds=1))
    validate_batch_window(_batch(entries=[inside]), _snapshot(), _end())
    validate_batch_window(_batch(entries=[_entry()]), _snapshot(), _end())


@pytest.mark.parametrize("changes", [
    {"coverage_statement": ""}, {"coverage_statement": "  "}, {"coverage_confirmed_at": None},
    {"entries": []}, {"entries": [_entry(), _entry()]}, {"extra": "not allowed"},
])
def test_batch_requires_explicit_confirmation_unique_ids_and_bounded_entries(changes):
    with pytest.raises(ValidationError):
        _batch(**changes)


@pytest.mark.parametrize("changes", [
    {"used_percent": "NaN"}, {"used_percent": "Infinity"}, {"used_percent": "-1"}, {"used_percent": "101"},
    {"window_duration_ms": True}, {"window_duration_ms": 604800000.0}, {"window_duration_ms": "604800000"},
    {"source": "guessed"}, {"account_key": "raw-account-id"},
    {"observed_at": "2026-09-14T04:00:00"}, {"resets_at": "2026-09-17T04:00:00"},
])
def test_snapshot_rejects_invalid_percent_units_identity_and_timestamps(changes):
    with pytest.raises(ValidationError):
        _snapshot(**changes)


def test_account_rejects_raw_identity_and_unknown_fields():
    account = PricingAccount(account_key=ACCOUNT, label="Work account", created_at=START)
    assert account.account_key == ACCOUNT
    with pytest.raises(ValidationError):
        PricingAccount(account_key="person@example.test", label="Work account", created_at=START)
    with pytest.raises(ValidationError):
        PricingAccount(account_key=ACCOUNT, label="Work account", created_at=START, email="person@example.test")


def test_decimal_delta_and_estimate_do_not_depend_on_ambient_precision():
    start = _snapshot(used_percent="10.0000000000000000000000000000001")
    end = _end(used_percent="12.0000000000000000000000000000002")
    expected = estimate_batch(_batch(), start, end, _catalog())
    with localcontext() as context:
        context.prec = 3
        actual = estimate_batch(_batch(), start, end, _catalog())
    assert actual.delta_used_percent == Decimal("2.0000000000000000000000000000001")
    assert actual.estimated_full_week_usd == expected.estimated_full_week_usd


@pytest.mark.parametrize("changes", [
    {"coverage": "local_only"}, {"coverage_confirmed_at": None},
    {"delta_used_percent": "1"}, {"estimated_full_week_usd": None}, {"status": "sample_only"},
])
def test_output_contract_cannot_claim_week_amount_without_conditions(changes):
    data = estimate_batch(_batch(), _snapshot(), _end(), _catalog()).model_dump()
    data.update(changes)
    with pytest.raises(ValidationError):
        PricingAccountEstimate.model_validate(data)
