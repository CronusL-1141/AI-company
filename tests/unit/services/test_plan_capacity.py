"""Native activity receipt times must never masquerade as coverage evidence."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic import ValidationError

from aiteam.services.plan_capacity import estimate_plan_capacity
from aiteam.types import PlanCapacityEstimate, PlanUsageSnapshot

BASE = datetime(2026, 9, 14, 4, 0, tzinfo=UTC)
SHORT_RESET = BASE + timedelta(hours=4)
SHORT_WINDOW = 18_000_000
WEEK_WINDOW = 604_800_000
MAX_SAFE = 9_007_199_254_740_991


def _snapshot(snapshot_id="start", minutes=0, tokens=1_000_000, percent=20, **changes: Any):
    observed = BASE + timedelta(minutes=minutes)
    return PlanUsageSnapshot.model_validate({
        "snapshot_id": snapshot_id, "account_key": "a" * 64, "limit_id": "codex",
        "window_duration_ms": SHORT_WINDOW, "resets_at": SHORT_RESET, "observed_at": observed,
        "used_percent": percent, "activity_observed_at": observed if tokens is not None else None,
        "activity_tokens": tokens, "activity_scope": "c" * 64 if tokens is not None else None,
        "source": "codex_account_activity", **changes,
    })


def _estimate(*snapshots, now=BASE + timedelta(minutes=20)):
    return estimate_plan_capacity(snapshots, now=now)[0]


def _local(snapshot_id="start", minutes=0, tokens=0, percent=20, **changes):
    changes.setdefault("activity_binding_at", BASE if tokens is not None else None)
    return _snapshot(
        snapshot_id, minutes, tokens, percent, source="codex_local_logs", **changes,
    )


def _assert_unknown_coverage(result, percent):
    assert result.status == "unavailable"
    assert result.reason_code == "activity_coverage_unknown"
    assert result.used_percent == percent
    assert result.estimated_total_tokens is None
    assert result.delta_tokens is None and result.delta_used_percent is None
    assert result.start_snapshot_id is None and result.interval_start is None


def test_positive_counter_and_percentage_changes_do_not_establish_coverage():
    result = _estimate(_snapshot(), _snapshot("end", minutes=10, tokens=2_000_000, percent=25))
    _assert_unknown_coverage(result, 25)
    assert result.end_snapshot_id == "end"
    assert result.observed_at == BASE + timedelta(minutes=10)


def test_receipt_times_equal_quota_observations_still_do_not_establish_coverage():
    start = _snapshot()
    end = _snapshot("end", minutes=10, tokens=2_000_000, percent=25)
    assert start.activity_observed_at == start.observed_at
    assert end.activity_observed_at == end.observed_at
    assert start.activity_scope == end.activity_scope
    _assert_unknown_coverage(_estimate(start, end), 25)


def test_delayed_counter_jump_after_hours_of_live_quota_changes_stays_unavailable():
    weekly = {"window_duration_ms": WEEK_WINDOW, "resets_at": BASE + timedelta(days=6)}
    snapshots = [
        _snapshot("morning", tokens=3_877_787_459, percent=34, **weekly),
        _snapshot("evening", minutes=240, tokens=3_877_787_459, percent=53, **weekly),
        _snapshot("delayed-update", minutes=250, tokens=3_978_787_459, percent=54, **weekly),
    ]
    _assert_unknown_coverage(_estimate(*snapshots, now=BASE + timedelta(minutes=260)), 54)


@pytest.mark.parametrize("tokens", [None, 0, 1_000_000, MAX_SAFE])
@pytest.mark.parametrize("percent", [0, 100])
def test_missing_zero_or_large_activity_preserves_percentage_without_capacity(tokens, percent):
    _assert_unknown_coverage(_estimate(_snapshot(tokens=tokens, percent=percent)), percent)


def test_accounts_buckets_and_short_weekly_windows_are_separate_and_stably_sorted():
    weekly = {"window_duration_ms": WEEK_WINDOW, "resets_at": BASE + timedelta(days=6)}
    snapshots = [
        _snapshot("a-short-0"), _snapshot("a-short-1", minutes=10, tokens=2_000_000, percent=25),
        _snapshot("a-week-0", percent=10, **weekly),
        _snapshot("a-week-1", minutes=10, tokens=2_000_000, percent=12, **weekly),
        _snapshot("b-only", account_key="b" * 64, percent=75),
        _snapshot("spark-0", limit_id="codex_spark"),
        _snapshot("spark-1", minutes=10, tokens=2_000_000, percent=25, limit_id="codex_spark"),
    ]
    results = estimate_plan_capacity(list(reversed(snapshots)), now=BASE + timedelta(minutes=20))
    assert results == estimate_plan_capacity(snapshots, now=BASE + timedelta(minutes=20))
    assert [(row.account_key, row.limit_id, row.window_duration_ms) for row in results] == sorted(
        (row.account_key, row.limit_id, row.window_duration_ms) for row in results
    )
    _assert_unknown_coverage(results[0], 25)
    _assert_unknown_coverage(results[1], 12)
    assert results[2].reason_code == "bucket_activity_unattributed"
    assert results[2].status == "unavailable" and results[2].used_percent == 25
    assert results[2].estimated_total_tokens is None and results[2].delta_tokens is None
    _assert_unknown_coverage(results[3], 75)


def test_only_latest_reset_is_used_without_reusing_old_window_baseline():
    old = _snapshot("old", minutes=-20, tokens=0, percent=0, resets_at=BASE - timedelta(minutes=1))
    current = _snapshot("current", minutes=5, tokens=1_000_000, percent=5)
    result = _estimate(old, current)
    assert result.resets_at == SHORT_RESET
    assert result.end_snapshot_id == "current"
    _assert_unknown_coverage(result, 5)


def test_latest_observation_can_move_reset_earlier_than_an_old_report():
    changed_reset = SHORT_RESET - timedelta(hours=1)
    result = _estimate(
        _snapshot("old-reset"),
        _snapshot("new-baseline", minutes=5, tokens=1_500_000, percent=22, resets_at=changed_reset),
        _snapshot("latest", minutes=10, tokens=2_500_000, percent=27, resets_at=changed_reset),
    )
    assert result.resets_at == changed_reset and result.end_snapshot_id == "latest"
    _assert_unknown_coverage(result, 27)


@pytest.mark.parametrize("changes", [
    {"activity_tokens": 1}, {"used_percent": 1}, {"activity_scope": "d" * 64},
])
def test_counter_percentage_or_scope_changes_do_not_create_alignment(changes):
    end = _snapshot("end", minutes=10, tokens=2_000_000, percent=25).model_copy(update=changes)
    _assert_unknown_coverage(_estimate(_snapshot(), end), end.used_percent)


def test_equal_observation_time_is_not_a_measured_interval():
    _assert_unknown_coverage(_estimate(_snapshot("a"), _snapshot("b", tokens=2_000_000, percent=25)), 25)


@pytest.mark.parametrize("now", [SHORT_RESET, SHORT_RESET + timedelta(microseconds=1)])
def test_expired_window_cannot_claim_current_percent(now):
    result = _estimate(_snapshot(), _snapshot("end", minutes=10, tokens=2_000_000, percent=25), now=now)
    assert result.status == "expired"
    assert result.used_percent is None and result.estimated_total_tokens is None
    assert result.delta_tokens is None and result.start_snapshot_id is None
    assert result.reason_code is None


def test_now_before_window_or_snapshot_in_future_is_unavailable():
    before_window = _estimate(_snapshot(), now=BASE - timedelta(hours=2))
    future_observation = _estimate(_snapshot("future", minutes=30))
    assert before_window.status == future_observation.status == "unavailable"
    assert before_window.used_percent is None and future_observation.used_percent is None


def test_inputs_are_independently_revalidated_even_after_model_copy():
    for changes in (
        {"activity_tokens": True}, {"activity_scope": None},
        {"activity_observed_at": BASE - timedelta(seconds=61)},
    ):
        with pytest.raises(ValidationError):
            _estimate(_snapshot().model_copy(update=changes))


@pytest.mark.parametrize("reason_code", ["activity_coverage_unknown", "bucket_activity_unattributed"])
def test_unavailable_reason_cannot_coexist_with_an_estimated_capacity(reason_code):
    payload = _estimate(_snapshot()).model_dump(mode="python")
    payload.update({
        "status": "estimated", "estimated_total_tokens": 20_000_000,
        "delta_tokens": 1_000_000, "delta_used_percent": 5,
        "start_snapshot_id": "earlier", "interval_start": BASE - timedelta(minutes=10),
        "reason_code": reason_code,
    })
    with pytest.raises(ValidationError):
        PlanCapacityEstimate.model_validate(payload)


def test_estimate_payload_without_new_reason_field_remains_readable():
    payload = _estimate(_snapshot()).model_dump(mode="python")
    payload.pop("reason_code")
    restored = PlanCapacityEstimate.model_validate(payload)
    assert restored.reason_code is None
    assert restored.used_percent == 20


@pytest.mark.parametrize(("field", "value"), [
    ("estimated_total_tokens", 20_000_000), ("delta_tokens", 1_000_000),
    ("delta_used_percent", 5), ("start_snapshot_id", "earlier"),
    ("interval_start", BASE - timedelta(minutes=10)),
])
def test_unavailable_reason_rejects_derived_fields(field, value):
    payload = _estimate(_snapshot()).model_dump(mode="python")
    payload[field] = value
    with pytest.raises(ValidationError):
        PlanCapacityEstimate.model_validate(payload)


def test_empty_input_is_empty_and_now_requires_timezone():
    assert estimate_plan_capacity([], now=BASE) == []
    with pytest.raises(ValueError, match="timezone-aware"):
        estimate_plan_capacity([], now=BASE.replace(tzinfo=None))


def test_local_zero_baseline_estimates_from_deduplicated_interval_tokens():
    start = _local()
    initial = _estimate(start)
    assert initial.status == "collecting"
    assert initial.source == "codex_local_logs"
    assert initial.reason_code is None
    result = _estimate(start, _local("end", minutes=10, tokens=1_000_000, percent=25))
    assert result.status == "estimated"
    assert result.estimated_total_tokens == 20_000_000
    assert result.source == "codex_local_logs" and result.reason_code is None
    assert result.used_percent == 25
    assert (result.delta_tokens, result.delta_used_percent) == (1_000_000, 5)
    assert (result.start_snapshot_id, result.end_snapshot_id) == ("start", "end")
    assert result.interval_start == BASE


def test_local_capacity_uses_earliest_continuous_baseline_and_integer_floor():
    result = _estimate(
        _local(percent=10), _local("middle", minutes=5, tokens=500, percent=11),
        _local("end", minutes=10, tokens=1000, percent=13),
    )
    assert result.status == "estimated"
    assert result.estimated_total_tokens == 33_333
    assert result.start_snapshot_id == "start"


@pytest.mark.parametrize("seconds", [1, 60, 299, 300])
def test_local_positive_intervals_have_no_minimum_duration(seconds):
    observed = BASE + timedelta(seconds=seconds)
    end = _local("end", tokens=1000, percent=25, observed_at=observed, activity_observed_at=observed)
    result = _estimate(_local(), end)
    assert result.status == "estimated"
    assert result.estimated_total_tokens == 20_000


def test_local_one_percentage_point_immediately_estimates_capacity():
    observed = BASE + timedelta(seconds=1)
    end = _local(
        "end", tokens=1_628_783, percent=21,
        observed_at=observed, activity_observed_at=observed,
    )
    result = _estimate(_local(), end)
    assert result.status == "estimated" and result.estimated_total_tokens == 162_878_300
    assert result.delta_tokens == 1_628_783 and result.delta_used_percent == 1


def test_local_equal_timestamps_do_not_form_a_positive_interval():
    result = _estimate(_local(), _local("same-time", tokens=1000, percent=21))
    assert result.status == "collecting"
    assert result.estimated_total_tokens is None and result.delta_tokens is None


@pytest.mark.parametrize(("tokens", "percent"), [(0, 25), (1000, 20), (0, 20)])
def test_local_zero_tokens_or_zero_percentage_change_remains_collecting(tokens, percent):
    result = _estimate(_local(), _local("end", minutes=10, tokens=tokens, percent=percent))
    assert result.status == "collecting" and result.estimated_total_tokens is None
    assert result.delta_tokens == tokens and result.delta_used_percent == percent - 20
    assert result.reason_code is None


def test_local_prediction_updates_with_new_samples_without_forcing_one_direction():
    start = _local()
    first = _local("first", minutes=1, tokens=1_000_000, percent=21)
    second = _local("second", minutes=2, tokens=3_000_000, percent=22)
    third = _local("third", minutes=3, tokens=3_300_000, percent=23)
    assert _estimate(start, first).estimated_total_tokens == 100_000_000
    assert _estimate(start, first, second).estimated_total_tokens == 150_000_000
    assert _estimate(start, first, second, third).estimated_total_tokens == 110_000_000


@pytest.mark.parametrize(("middle_tokens", "middle_percent"), [(500, 22), (2000, 10)])
def test_local_counter_or_percentage_rollback_restarts_baseline(middle_tokens, middle_percent):
    result = _estimate(
        _local(tokens=1000),
        _local("rebuilt", minutes=5, tokens=middle_tokens, percent=middle_percent),
        _local("end", minutes=10, tokens=middle_tokens + 1000, percent=middle_percent + 5),
    )
    assert result.status == "estimated" and result.estimated_total_tokens == 20_000
    assert result.start_snapshot_id == "rebuilt"
    assert result.delta_tokens == 1000 and result.delta_used_percent == 5


def test_local_latest_rollback_cannot_reuse_previous_baseline():
    result = _estimate(_local(tokens=1000), _local("rollback", minutes=10, tokens=500, percent=25))
    assert result.status == "collecting"
    assert result.start_snapshot_id is None and result.delta_tokens is None


@pytest.mark.parametrize("changes", [
    {"activity_scope": "d" * 64}, {"activity_binding_at": BASE + timedelta(minutes=5)},
])
def test_local_scope_or_binding_change_rebuilds_baseline(changes):
    result = _estimate(
        _local(), _local("rebuilt", minutes=5, tokens=1000, percent=25, **changes),
        _local("end", minutes=10, tokens=2000, percent=30, **changes),
    )
    assert result.status == "estimated" and result.estimated_total_tokens == 20_000
    assert result.start_snapshot_id == "rebuilt" and result.delta_tokens == 1000


def test_local_missing_activity_breaks_continuity_and_latest_missing_is_unavailable():
    missing = _local("missing", minutes=5, tokens=None, percent=22)
    result = _estimate(_local(), missing)
    assert result.status == "unavailable" and result.reason_code == "local_usage_unavailable"
    assert result.used_percent == 22 and result.estimated_total_tokens is None
    assert result.start_snapshot_id is None and result.delta_tokens is None
    rebuilt = _local("rebuilt", minutes=10, tokens=1000, percent=25)
    assert _estimate(_local(), missing, rebuilt).status == "collecting"
    result = _estimate(_local(), missing, rebuilt, _local("end", minutes=15, tokens=2000, percent=30))
    assert result.status == "estimated" and result.start_snapshot_id == "rebuilt"
    assert result.estimated_total_tokens == 20_000


def test_raw_summary_interrupts_local_continuity_and_is_never_mixed_into_delta():
    raw = _snapshot("raw", minutes=5, tokens=3_000_000_000, percent=22)
    rebuilt = _local("rebuilt", minutes=10, tokens=1000, percent=25)
    first = _estimate(_local(), raw, rebuilt)
    assert first.status == "collecting" and first.delta_tokens is None
    result = _estimate(_local(), raw, rebuilt, _local("end", minutes=15, tokens=2000, percent=30))
    assert result.status == "estimated" and result.source == "codex_local_logs"
    assert result.start_snapshot_id == "rebuilt" and result.delta_tokens == 1000
    _assert_unknown_coverage(_estimate(_local(), raw), 22)


def test_local_only_pairs_within_same_account_bucket_window_and_reset():
    weekly = {"window_duration_ms": WEEK_WINDOW, "resets_at": BASE + timedelta(days=6)}
    values = [
        _local("short-0"), _local("short-1", minutes=10, tokens=1000, percent=25),
        _local("week-0", percent=10, **weekly),
        _local("week-1", minutes=10, tokens=1000, percent=12, **weekly),
        _local("other-account", account_key="b" * 64),
        _local("model-bucket", limit_id="codex_spark"),
    ]
    results = estimate_plan_capacity(values, now=BASE + timedelta(minutes=20))
    assert [result.estimated_total_tokens for result in results] == [20_000, 50_000, None, None]
    assert results[2].reason_code == "bucket_activity_unattributed"
    assert results[3].status == "collecting"
    changed = _local("new-reset", minutes=10, tokens=1000, percent=5, resets_at=SHORT_RESET - timedelta(hours=1))
    assert _estimate(_local(), changed).status == "collecting"


def test_local_safe_integer_limit_and_overflow():
    exact = _estimate(_local(percent=0), _local("end", minutes=10, tokens=MAX_SAFE, percent=100))
    assert exact.status == "estimated" and exact.estimated_total_tokens == MAX_SAFE
    overflow = _estimate(_local(percent=0), _local("end", minutes=10, tokens=MAX_SAFE, percent=2))
    assert overflow.status == "unavailable" and overflow.estimated_total_tokens is None


def test_local_expired_window_never_shows_stale_percentage():
    result = _estimate(_local(), _local("end", minutes=10, tokens=1000, percent=25), now=SHORT_RESET)
    assert result.status == "expired" and result.used_percent is None
    assert result.estimated_total_tokens is None and result.delta_tokens is None
