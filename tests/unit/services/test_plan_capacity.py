"""Token-only plan estimates retain native percentages and evidence boundaries."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic import ValidationError

from aiteam.services.plan_capacity import estimate_plan_capacity
from aiteam.types import PlanUsageSnapshot

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


def test_one_million_tokens_over_five_points_estimates_twenty_million():
    result = _estimate(_snapshot(), _snapshot("end", minutes=10, tokens=2_000_000, percent=25))
    assert result.status == "estimated"
    assert result.estimated_total_tokens == 20_000_000
    assert result.used_percent == 25
    assert (result.delta_tokens, result.delta_used_percent) == (1_000_000, 5)
    assert (result.start_snapshot_id, result.end_snapshot_id) == ("start", "end")
    assert result.interval_start == BASE
    assert result.observed_at == BASE + timedelta(minutes=10)


def test_capacity_floor_uses_integer_arithmetic_and_earliest_continuous_baseline():
    result = _estimate(
        _snapshot(tokens=0, percent=10),
        _snapshot("middle", minutes=5, tokens=500, percent=11),
        _snapshot("end", minutes=10, tokens=1000, percent=13),
    )
    assert result.estimated_total_tokens == 33_333
    assert result.start_snapshot_id == "start"


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
    assert [row.estimated_total_tokens for row in results] == [20_000_000, 50_000_000, None, None]
    assert results[2].status == "unavailable" and results[2].used_percent == 25
    assert results[2].delta_tokens is None and results[2].start_snapshot_id is None
    assert results[3].status == "collecting" and results[3].used_percent == 75


def test_only_latest_reset_is_used_without_reusing_old_window_baseline():
    old = _snapshot(
        "old", minutes=-20, tokens=0, percent=0, resets_at=BASE - timedelta(minutes=1),
    )
    current = _snapshot("current", minutes=5, tokens=1_000_000, percent=5)
    result = _estimate(old, current)
    assert result.resets_at == SHORT_RESET
    assert result.status == "collecting" and result.used_percent == 5
    assert result.estimated_total_tokens is None and result.start_snapshot_id is None


def test_latest_observation_can_move_reset_earlier_than_an_old_report():
    changed_reset = SHORT_RESET - timedelta(hours=1)
    result = _estimate(
        _snapshot("old-reset"),
        _snapshot("new-baseline", minutes=5, tokens=1_500_000, percent=22, resets_at=changed_reset),
        _snapshot("latest", minutes=10, tokens=2_500_000, percent=27, resets_at=changed_reset),
    )
    assert result.resets_at == changed_reset and result.used_percent == 27
    assert result.status == "estimated" and result.estimated_total_tokens == 20_000_000
    assert result.start_snapshot_id == "new-baseline" and result.end_snapshot_id == "latest"


@pytest.mark.parametrize(("first_tokens", "first_percent", "middle_tokens", "middle_percent"), [
    (1000, 10, 500, 11), (1000, 20, 1500, 11),
])
def test_counter_or_percent_rollback_rebuilds_baseline(
    first_tokens, first_percent, middle_tokens, middle_percent,
):
    result = _estimate(
        _snapshot(tokens=first_tokens, percent=first_percent),
        _snapshot("rebuilt", minutes=5, tokens=middle_tokens, percent=middle_percent),
        _snapshot("end", minutes=10, tokens=middle_tokens + 1000, percent=middle_percent + 5),
    )
    assert result.status == "estimated" and result.estimated_total_tokens == 20_000
    assert result.start_snapshot_id == "rebuilt"
    assert result.delta_tokens == 1000 and result.delta_used_percent == 5


def test_latest_rollback_does_not_fabricate_zero_deltas():
    result = _estimate(_snapshot(), _snapshot("end", minutes=10, tokens=1, percent=25))
    assert result.status == "collecting" and result.used_percent == 25
    assert result.start_snapshot_id is None and result.interval_start is None
    assert result.delta_tokens is None and result.delta_used_percent is None


def test_scope_change_restarts_instead_of_joining_incomparable_counters():
    result = _estimate(
        _snapshot(),
        _snapshot("scope-change", minutes=5, tokens=2_000_000, percent=25, activity_scope="d" * 64),
        _snapshot("end", minutes=10, tokens=3_000_000, percent=30, activity_scope="d" * 64),
    )
    assert result.start_snapshot_id == "scope-change"
    assert result.estimated_total_tokens == 20_000_000


def test_missing_activity_breaks_segment_and_new_valid_points_rebuild_it():
    snapshots = [
        _snapshot(), _snapshot("missing", minutes=5, tokens=None, percent=22),
        _snapshot("rebuilt", minutes=10, tokens=1_500_000, percent=25),
    ]
    collecting = _estimate(*snapshots)
    assert collecting.status == "collecting" and collecting.delta_tokens is None
    result = _estimate(*snapshots, _snapshot("end", minutes=15, tokens=2_500_000, percent=30))
    assert result.start_snapshot_id == "rebuilt" and result.estimated_total_tokens == 20_000_000


@pytest.mark.parametrize("percent", [0, 100])
def test_unknown_activity_preserves_native_percentage_without_fake_tokens(percent):
    result = _estimate(_snapshot(tokens=None, percent=percent))
    assert result.used_percent == percent and result.status == "collecting"
    assert result.estimated_total_tokens is None and result.delta_tokens is None
    assert result.delta_used_percent is None


@pytest.mark.parametrize(("tokens", "percent"), [(1_000_000, 25), (2_000_000, 20), (2_000_000, 21)])
def test_zero_or_one_point_changes_collect_without_predicting_zero(tokens, percent):
    result = _estimate(_snapshot(), _snapshot("end", minutes=10, tokens=tokens, percent=percent))
    assert result.status == "collecting" and result.estimated_total_tokens is None
    assert result.delta_tokens == tokens - 1_000_000
    assert result.delta_used_percent == percent - 20


@pytest.mark.parametrize(("seconds", "status"), [(299, "collecting"), (300, "estimated")])
def test_minimum_five_minute_interval(seconds, status):
    observed = BASE + timedelta(seconds=seconds)
    end = _snapshot(
        "end", tokens=2_000_000, percent=25, observed_at=observed, activity_observed_at=observed,
    )
    assert _estimate(_snapshot(), end).status == status


def test_equal_observation_time_is_not_a_measured_interval():
    result = _estimate(_snapshot("a"), _snapshot("b", tokens=2_000_000, percent=25))
    assert result.status == "collecting" and result.start_snapshot_id is None
    assert result.delta_tokens is None


@pytest.mark.parametrize("now", [SHORT_RESET, SHORT_RESET + timedelta(microseconds=1)])
def test_expired_window_cannot_claim_current_percent(now):
    result = _estimate(_snapshot(), _snapshot("end", minutes=10, tokens=2_000_000, percent=25), now=now)
    assert result.status == "expired"
    assert result.used_percent is None and result.estimated_total_tokens is None
    assert result.delta_tokens is None and result.start_snapshot_id is None


def test_now_before_window_or_snapshot_in_future_is_unavailable():
    before_window = _estimate(_snapshot(), now=BASE - timedelta(hours=2))
    future_observation = _estimate(_snapshot("future", minutes=30))
    assert before_window.status == future_observation.status == "unavailable"
    assert before_window.used_percent is None and future_observation.used_percent is None


def test_snapshot_before_window_start_cannot_supply_a_baseline():
    old = _snapshot("too-old", minutes=-61, tokens=0, percent=0)
    result = _estimate(old, _snapshot("current", minutes=10, tokens=2_000_000, percent=25))
    assert result.status == "collecting" and result.delta_tokens is None


def test_safe_integer_ceiling_accepts_exact_limit_and_rejects_extrapolation_overflow():
    start = _snapshot(tokens=0, percent=0)
    exact = _estimate(start, _snapshot("exact", minutes=10, tokens=MAX_SAFE, percent=100))
    assert exact.estimated_total_tokens == MAX_SAFE and exact.status == "estimated"
    overflow = _estimate(start, _snapshot("overflow", minutes=10, tokens=MAX_SAFE, percent=2))
    assert overflow.estimated_total_tokens is None and overflow.status == "unavailable"
    assert overflow.delta_tokens == MAX_SAFE and overflow.delta_used_percent == 2


def test_inputs_are_independently_revalidated_even_after_model_copy():
    bad = _snapshot().model_copy(update={"activity_tokens": True})
    with pytest.raises(ValidationError):
        _estimate(bad)
    bad_scope = _snapshot().model_copy(update={"activity_scope": None})
    with pytest.raises(ValidationError):
        _estimate(bad_scope)
    stale_activity = _snapshot().model_copy(update={"activity_observed_at": BASE - timedelta(seconds=61)})
    with pytest.raises(ValidationError):
        _estimate(stale_activity)


def test_empty_input_is_empty_and_now_requires_timezone():
    assert estimate_plan_capacity([], now=BASE) == []
    with pytest.raises(ValueError, match="timezone-aware"):
        estimate_plan_capacity([], now=BASE.replace(tzinfo=None))
