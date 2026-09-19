"""Independent monetary plan contracts preserve exact prices and coverage."""

from datetime import timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from aiteam.services.plan_pricing import estimate_pricing_plan_capacity, pricing_sample_total
from aiteam.types import PricingPlanCapacityEstimate, PricingPlanSample, PricingPlanSnapshot

from .._plan_pricing_fixtures import BASE, catalog, entry, price_snapshot, sample


def estimate(*snapshots):
    return estimate_pricing_plan_capacity(snapshots, now=BASE + timedelta(hours=1))[0]


def test_mixed_models_cache_and_output_give_exact_one_percent_dollar_capacity():
    end = BASE + timedelta(seconds=1)
    latest = price_snapshot("end", at=end, percent=21, entries=[entry(), entry("b", model="model-b")])
    assert pricing_sample_total(latest.pricing) == Decimal(".01464")
    result = estimate(price_snapshot(), latest)
    assert result.status == "estimated"
    assert result.delta_usd == Decimal(".01464")
    assert result.estimated_total_usd == Decimal("1.464")
    assert result.delta_used_percent == 1
    assert result.model_dump(mode="json")["estimated_total_usd"] == "1.46400"
    assert result.pricing_mode == "standard_equivalent" and result.catalog_sha256 == latest.pricing.catalog_sha256


def test_context_rate_is_selected_per_request_before_sum():
    end = BASE + timedelta(seconds=1)
    small = entry("small", input_tokens=100000, cached_input_tokens=100000, cache_write_input_tokens=0, output_tokens=0)
    large = entry("large", input_tokens=100001, cached_input_tokens=100001, cache_write_input_tokens=0, output_tokens=0)
    quoted = sample(end=end, entries=[small, large])
    assert [item.rate_record.min_input_tokens for item in quoted.quotes[0].items] == [0, 100001]
    assert pricing_sample_total(quoted) == Decimal(".0600004")


def test_missing_one_model_contributes_zero_and_known_prices_still_predict():
    latest = price_snapshot("end", at=BASE + timedelta(seconds=1), percent=21,
                            entries=[entry(), entry("unknown", model="unknown-model")])
    assert latest.pricing.quotes[0].priced_subtotal_usd > 0
    assert latest.activity_usd is None
    result = estimate(price_snapshot(), latest)
    assert result.status == "estimated" and result.reason_code == "pricing_incomplete"
    assert result.estimated_total_usd == Decimal(".294") and result.delta_usd == Decimal(".00294")
    assert result.prediction_basis == "cycle_anchor_missing_zero"


def test_incomplete_reader_may_preserve_complete_known_quotes_without_claiming_total():
    data = sample(end=BASE + timedelta(seconds=1), entries=[entry()], complete=False)
    assert data.quotes[0].complete and not data.complete
    assert pricing_sample_total(data) is None


@pytest.mark.parametrize("changes", [
    {"pricing_mode": "logged_tier"}, {"complete": "true"}, {"catalog_sha256": "c" * 64},
])
def test_sample_rejects_wrong_mode_flag_or_catalog(changes):
    payload = sample(end=BASE + timedelta(seconds=1), entries=[entry()]).model_dump(mode="python")
    with pytest.raises(ValidationError):
        PricingPlanSample.model_validate({**payload, **changes})


def test_duplicate_response_and_wrong_interval_or_quote_identity_are_rejected():
    data = sample(end=BASE + timedelta(seconds=1), entries=[entry()]).model_dump(mode="python")
    for mutate in ("duplicate", "interval", "identity"):
        payload = sample(end=BASE + timedelta(seconds=1), entries=[entry()]).model_dump(mode="python")
        if mutate == "duplicate":
            payload["entries"].append(data["entries"][0])
        elif mutate == "interval":
            payload["interval_start"] = payload["interval_end"]
        else:
            payload["quotes"][0]["items"][0]["model"] = "model-b"
        with pytest.raises(ValidationError):
            PricingPlanSample.model_validate(payload)


def test_persisted_quote_amount_is_recomputed_from_the_same_production_rate_function():
    payload = sample(end=BASE + timedelta(seconds=1), entries=[entry()]).model_dump(mode="python")
    quote = payload["quotes"][0]
    quote["items"][0]["amount_usd"] = Decimal(".1")
    quote["total_usd"] = quote["priced_subtotal_usd"] = Decimal(".1")
    with pytest.raises(ValueError, match="exact rate record"):
        pricing_sample_total(PricingPlanSample.model_validate(payload))


def test_catalog_change_and_gap_preserve_the_cycle_anchor():
    changed = price_snapshot("changed", at=BASE + timedelta(seconds=1), percent=21, price_catalog=catalog("v2"),
                             binding=BASE + timedelta(seconds=1), start=BASE + timedelta(seconds=1))
    assert estimate(price_snapshot(), changed).estimated_total_usd == 0
    later_at = BASE + timedelta(seconds=2)
    later = price_snapshot("later", at=later_at, percent=22, price_catalog=catalog("v2"),
                           binding=changed.observed_at, start=changed.observed_at, entries=[entry(at=later_at)])
    assert estimate(price_snapshot(), changed, later).estimated_total_usd == Decimal(".147")
    assert estimate(price_snapshot(), changed, later).start_snapshot_id == "baseline"
    gap = price_snapshot("gap", at=BASE + timedelta(seconds=1), percent=21, unavailable=True)
    assert estimate(price_snapshot(), gap).reason_code == "pricing_unavailable"


def test_non_contiguous_interval_counts_known_prices_without_moving_anchor():
    latest = price_snapshot("later", at=BASE + timedelta(seconds=2), start=BASE + timedelta(seconds=1),
                            percent=21, entries=[entry(at=BASE + timedelta(seconds=2))])
    assert estimate(price_snapshot(), latest).estimated_total_usd == Decimal(".294")
    assert estimate(price_snapshot(), latest).start_snapshot_id == "baseline"


def test_positive_percentage_can_predict_zero_and_zero_percentage_remains_collecting():
    baseline = price_snapshot()
    assert baseline.activity_usd == 0
    assert estimate(baseline).status == "collecting"
    no_usage = price_snapshot("empty", at=BASE + timedelta(seconds=1), percent=21)
    assert estimate(baseline, no_usage).estimated_total_usd == 0
    no_percent = price_snapshot("priced", at=BASE + timedelta(seconds=1), entries=[entry()])
    assert estimate(baseline, no_percent).status == "collecting"


def test_separate_bucket_never_uses_account_price_and_expiry_hides_percentage():
    result = estimate(price_snapshot(limit_id="codex_spark"))
    assert result.reason_code == "bucket_activity_unattributed"
    assert result.estimated_total_usd is None
    old = price_snapshot()
    result = estimate_pricing_plan_capacity([old], now=old.resets_at)[0]
    assert result.status == "expired" and result.used_percent is None


@pytest.mark.parametrize("changes", [{"activity_usd": 0.1}, {"activity_usd": "1", "pricing": None}])
def test_price_snapshot_cannot_use_floats_or_unproven_amounts(changes):
    with pytest.raises(ValidationError):
        PricingPlanSnapshot.model_validate({**price_snapshot().model_dump(mode="python"), **changes})


def test_legacy_estimates_keep_strict_metadata_reason_and_positive_value_rules():
    latest = price_snapshot("end", at=BASE + timedelta(seconds=1), percent=21, entries=[entry()])
    result = estimate(price_snapshot(), latest).model_dump(mode="python")
    result.pop("prediction_basis")
    for changes in ({"catalog_sha256": None}, {"reason_code": "pricing_incomplete"}, {"delta_usd": "0"}):
        with pytest.raises(ValidationError):
            PricingPlanCapacityEstimate.model_validate({**result, **changes})


def priced_history():
    baseline = price_snapshot()
    first = price_snapshot("first", at=BASE + timedelta(seconds=1), percent=21, entries=[entry()])
    at = BASE + timedelta(seconds=2)
    second = price_snapshot(
        "second", at=at, start=first.observed_at, previous_usd=first.activity_usd,
        percent=22, entries=[entry("b", at=at, model="model-b")],
    )
    return baseline, first, second


def incomplete_after(previous, *, percent=23, **changes):
    at = previous.observed_at + timedelta(seconds=1)
    return price_snapshot(
        "missing-model", at=at, start=previous.observed_at, percent=percent,
        entries=[entry("missing", at=at, model="unknown-model")], **changes,
    )


def test_multiple_points_use_earliest_continuous_baseline_not_adjacent_pair():
    result = estimate(*priced_history())
    assert result.start_snapshot_id == "baseline"
    assert result.delta_usd == Decimal(".01464") and result.delta_used_percent == 2
    assert result.estimated_total_usd == Decimal(".732")
    assert result.last_estimated_total_usd == result.estimated_total_usd
    assert result.last_estimate_observed_at == BASE + timedelta(seconds=2)


def test_missing_model_recalculates_using_all_cycle_percentage_points():
    history = priced_history()
    missing = incomplete_after(history[-1], percent=30)
    result = estimate(*history, missing)
    assert result.status == "estimated" and result.reason_code == "pricing_incomplete"
    assert result.used_percent == 30
    assert result.estimated_total_usd == Decimal(".1464") and result.delta_usd == Decimal(".01464")
    assert result.start_snapshot_id == "baseline"
    assert result.last_estimated_total_usd == result.estimated_total_usd
    assert result.last_estimate_observed_at == missing.observed_at


def test_old_partial_rebinding_does_not_move_the_cycle_anchor():
    history = priced_history()
    missing = incomplete_after(history[-1])
    at = missing.observed_at + timedelta(seconds=1)
    restarted = price_snapshot("restart", at=at, start=at, binding=at, percent=23)
    collecting = estimate(*history, missing, restarted)
    assert collecting.status == "estimated" and collecting.estimated_total_usd == Decimal(".488")
    assert collecting.start_snapshot_id == "baseline"
    end = at + timedelta(seconds=1)
    new = price_snapshot("new", at=end, start=at, binding=at, percent=24, entries=[entry("new", at=end)])
    current = estimate(*history, missing, restarted, new)
    assert current.estimated_total_usd == Decimal(".4395")
    assert current.start_snapshot_id == "baseline"
    later = incomplete_after(new, percent=25, binding=at)
    retained = estimate(*history, missing, restarted, new, later)
    assert retained.last_estimated_total_usd == Decimal(".3516")
    assert retained.last_estimate_observed_at == later.observed_at


@pytest.mark.parametrize("change", ["percent", "scope", "catalog", "reset", "missing_metadata"])
def test_only_percentage_drop_or_reset_change_clears_the_cycle(change):
    history = priced_history()
    if change == "missing_metadata":
        latest = price_snapshot("unknown", at=BASE + timedelta(seconds=3), percent=23, unavailable=True)
    else:
        updates = {"scope": "d" * 64} if change == "scope" else {}
        if change == "catalog":
            updates["price_catalog"] = catalog("fixture-v2")
        latest = incomplete_after(history[-1], percent=21 if change == "percent" else 23, **updates)
        if change == "reset":
            latest = latest.model_copy(update={"resets_at": latest.resets_at + timedelta(days=1)})
    result = estimate(*history, latest)
    if change in ("percent", "reset"):
        assert result.status == "collecting" and result.last_estimated_total_usd is None
        assert result.start_snapshot_id == latest.snapshot_id
    else:
        assert result.estimated_total_usd == Decimal(".488") and result.start_snapshot_id == "baseline"


def test_reset_change_then_return_does_not_restore_an_earlier_cycle_value():
    history = priced_history()
    changed = incomplete_after(history[-1]).model_copy(update={"resets_at": BASE + timedelta(days=4)})
    returned = incomplete_after(changed, percent=24)
    result = estimate(*history, changed, returned)
    assert result.last_estimated_total_usd is None


def test_percentage_rollback_rebuilds_current_baseline_and_drops_old_value():
    history = priced_history()
    at = BASE + timedelta(seconds=3)
    lower = price_snapshot("lower", at=at, start=at, binding=at, percent=10)
    assert estimate(*history, lower).last_estimated_total_usd is None
    end = at + timedelta(seconds=1)
    later = price_snapshot("later", at=end, start=at, binding=at, percent=11, entries=[entry("later", at=end)])
    result = estimate(*history, lower, later)
    assert result.start_snapshot_id == "lower" and result.estimated_total_usd == Decimal(".294")


def test_no_valid_history_or_expired_window_has_no_retained_value():
    latest = incomplete_after(price_snapshot())
    assert estimate(price_snapshot(), latest).last_estimated_total_usd == 0
    history = priced_history()
    expired = estimate_pricing_plan_capacity(history, now=history[-1].resets_at)[0]
    assert expired.status == "expired" and expired.last_estimated_total_usd is None


@pytest.mark.parametrize("changes", [
    {"last_estimated_total_usd": None}, {"last_estimate_observed_at": None},
    {"last_estimated_total_usd": "-1"}, {"last_estimated_total_usd": 0.1},
    {"last_estimate_observed_at": BASE + timedelta(days=1)},
])
def test_retained_value_and_original_asof_require_a_valid_pair(changes):
    history = priced_history()
    value = estimate(*history, incomplete_after(history[-1])).model_dump(mode="python")
    with pytest.raises(ValidationError):
        PricingPlanCapacityEstimate.model_validate({**value, **changes})


def test_old_result_without_history_fields_remains_valid():
    value = estimate(price_snapshot()).model_dump(mode="python")
    value.pop("last_estimated_total_usd")
    value.pop("last_estimate_observed_at")
    restored = PricingPlanCapacityEstimate.model_validate(value)
    assert restored.last_estimated_total_usd is None and restored.last_estimate_observed_at is None


def test_all_missing_prices_predict_zero_with_explicit_basis_without_creating_actual_values():
    baseline = price_snapshot(unavailable=True)
    latest = price_snapshot("empty", at=BASE + timedelta(seconds=1), percent=21, unavailable=True)
    result = estimate(baseline, latest)
    assert result.estimated_total_usd == result.delta_usd == 0
    assert result.prediction_basis == "cycle_anchor_missing_zero"
    assert result.catalog_sha256 is None and result.reason_code == "pricing_unavailable"
    assert baseline.activity_usd is None and latest.activity_usd is None


def test_overlapping_old_intervals_count_each_known_request_once():
    baseline, first, _ = priced_history()
    later = price_snapshot("overlap", at=BASE + timedelta(seconds=2), percent=22,
                           entries=[entry(), entry("next", at=BASE + timedelta(seconds=2))])
    result = estimate(baseline, first, later)
    assert result.delta_usd == Decimal(".00588") and result.estimated_total_usd == Decimal(".294")


def test_account_and_window_groups_never_share_cycle_contributions():
    baseline, first, _ = priced_history()
    other = price_snapshot("foreign", key="d" * 64, at=BASE + timedelta(seconds=2),
                           percent=90, entries=[entry("foreign", at=BASE + timedelta(seconds=2))])
    results = estimate_pricing_plan_capacity([baseline, first, other], now=BASE + timedelta(hours=1))
    assert results[0].estimated_total_usd == Decimal(".294")
    assert results[1].status == "collecting" and results[1].start_snapshot_id == "foreign"
