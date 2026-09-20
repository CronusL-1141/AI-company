"""Fenced account monitoring must survive workers, cancellation, and restarts."""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import timedelta
from decimal import Decimal

import pytest

from aiteam.clock import from_timestamp
from aiteam.storage.account_monitor import MonitorRepository
from aiteam.storage.account_usage import AccountUsageRepository
from aiteam.storage.engine_pool import engine_pool
from aiteam.storage.models import AccountUsageMonitorModel
from aiteam.types import PlanUsageSnapshot, PricingAccount, PricingMonitorSettings, PricingQuotaSnapshot

KEY = "a" * 64
OTHER_KEY = "b" * 64
START = from_timestamp(1800000000)
INTERVAL_MS = 300000


def account(key=KEY, label="Account"):
    return PricingAccount(account_key=key, label=label, created_at=START)


def snapshot(identifier="sample", key=KEY, observed_at=START):
    return PricingQuotaSnapshot(
        snapshot_id=identifier, account_key=key, limit_id="codex",
        used_percent="20", window_duration_ms=604800000,
        resets_at=START + timedelta(days=2), observed_at=observed_at,
        source="codex_app_server",
    )


def enabled():
    return PricingMonitorSettings(enabled=True, interval_ms=INTERVAL_MS)


@pytest.fixture
async def stores(tmp_path, monkeypatch):
    db_path = tmp_path / "monitor.db"
    db_url = f"sqlite+aiosqlite:///{db_path}"
    accounts = AccountUsageRepository(db_url)
    await accounts.init_db()
    await accounts.upsert_account(account())
    await accounts.upsert_account(account(OTHER_KEY))
    first, second = MonitorRepository(db_url), MonitorRepository(db_url)
    await first.init_db()
    now = [START]
    monkeypatch.setattr("aiteam.storage.account_monitor.utc_now", lambda: now[0])
    yield first, second, accounts, now, db_path
    await engine_pool.get_engine(db_url).dispose()


def monitor_rows(db_path):
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as connection:
        return connection.execute("SELECT * FROM account_usage_monitors").fetchall()


async def test_init_db_creates_only_its_single_table(tmp_path):
    db_path = tmp_path / "only-monitor.db"
    db_url = f"sqlite+aiosqlite:///{db_path}"
    first, second = MonitorRepository(db_url), MonitorRepository(db_url)
    try:
        await asyncio.gather(first.init_db(), second.init_db())
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as connection:
            tables = {row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'",
            )}
        assert tables == {"account_usage_monitors"}
    finally:
        await engine_pool.get_engine(db_url).dispose()


@pytest.mark.parametrize("db_url", [None, "", " ", "postgresql+asyncpg://localhost/test"])
def test_database_selection_must_be_explicit_sqlite(db_url):
    with pytest.raises(ValueError):
        MonitorRepository(db_url)


@pytest.mark.parametrize("interval_ms", [29999, 1800001, 86400001, "300000", 300000.0, True])
def test_interval_requires_bounded_integer_milliseconds(interval_ms):
    with pytest.raises(ValueError):
        PricingMonitorSettings(interval_ms=interval_ms)


@pytest.mark.parametrize("interval_ms", [30000, 1800000])
def test_new_interval_limits_are_inclusive(interval_ms):
    assert PricingMonitorSettings(interval_ms=interval_ms).interval_ms == interval_ms


async def store_legacy_interval(repository, interval_ms=86400000):
    """Install an old payload only in the test's isolated database."""
    await repository.configure(KEY, enabled())
    async with repository._write_session() as session:
        row = await session.get(AccountUsageMonitorModel, KEY)
        row.payload = {**row.payload, "settings": {"enabled": True, "interval_ms": interval_ms}}


async def test_legacy_period_reads_unchanged_and_pauses_without_shortening(stores):
    first, second, _, _, db_path = stores
    await store_legacy_interval(first)
    before = monitor_rows(db_path)
    state = await second.get(KEY)
    assert state.settings.interval_ms == 86400000 and state.settings.enabled
    assert monitor_rows(db_path) == before
    paused = await second.configure(KEY, PricingMonitorSettings(enabled=False))
    assert paused.settings.interval_ms == 86400000
    assert paused.status == "disabled" and paused.next_run_at is None
    assert (await first.get(KEY)).settings.interval_ms == 86400000
    with pytest.raises(ValueError, match="明确选择"):
        await first.configure(KEY, PricingMonitorSettings(enabled=True))
    assert (await second.get(KEY)).revision == paused.revision
    updated = await first.configure(KEY, PricingMonitorSettings(enabled=True, interval_ms=30000))
    assert updated.settings.interval_ms == 30000 and updated.settings.enabled


@pytest.mark.parametrize("interval_ms", [86400001, "86400000", 86400000.0, True])
async def test_legacy_context_does_not_accept_bad_types_or_unbounded_periods(stores, interval_ms):
    first, second, _, _, _ = stores
    await store_legacy_interval(first, interval_ms)
    with pytest.raises(ValueError):
        await second.get(KEY)


@pytest.mark.parametrize("outcome", ["success", "error", "cancel"])
async def test_legacy_claim_renew_and_finish_preserve_original_period(stores, outcome):
    first, second, _, now, _ = stores
    await store_legacy_interval(first)
    claim = await second.claim_due("legacy-worker", now[0])
    assert claim["state"].settings.interval_ms == 86400000
    now[0] += timedelta(seconds=10)
    assert await first.renew_claim(claim, now[0]) is True
    if outcome == "cancel":
        assert await first.release_claim(claim) is True
    elif outcome == "error":
        assert await first.finish(claim, None, [], error="Capture unavailable.") is True
    else:
        assert await first.finish(claim, account(), [snapshot()]) is True
    state = await second.get(KEY)
    assert state.settings.interval_ms == 86400000
    assert state.next_run_at == now[0] + timedelta(days=1)


async def test_omitting_period_preserves_existing_five_minute_setting(stores):
    first, second, _, _, _ = stores
    await first.configure(KEY, enabled())
    paused = await first.configure(KEY, PricingMonitorSettings(enabled=False))
    assert paused.settings.interval_ms == 300000
    resumed = await second.configure(KEY, PricingMonitorSettings(enabled=True))
    assert resumed.settings.interval_ms == 300000


async def test_get_defaults_to_disabled_without_creating_work(stores):
    first, _, _, _, db_path = stores
    state = await first.get(KEY)
    assert state.settings == PricingMonitorSettings(enabled=False, interval_ms=1800000)
    assert state.revision == 0
    assert state.status == "disabled"
    assert state.next_run_at is None
    assert state.runtime_running is False
    assert monitor_rows(db_path) == []


async def test_missing_account_cannot_read_or_configure_monitor(stores):
    first, _, _, _, db_path = stores
    missing = "f" * 64
    with pytest.raises(ValueError, match="account does not exist"):
        await first.get(missing)
    with pytest.raises(ValueError, match="account does not exist"):
        await first.configure(missing, enabled())
    assert monitor_rows(db_path) == []


async def test_settings_revision_and_due_time_cross_sessions(stores):
    first, second, _, now, _ = stores
    state = await first.configure(KEY, enabled())
    assert state.revision == 1
    assert state.status == "waiting"
    assert state.next_run_at == now[0]
    assert await second.get(KEY) == state
    disabled = await second.configure(KEY, PricingMonitorSettings())
    assert disabled.revision == 2
    assert disabled.next_run_at is None
    assert disabled.status == "disabled"
    again = await first.configure(KEY, PricingMonitorSettings())
    assert again.revision == 3


async def test_only_one_account_can_enable_native_monitor(stores):
    first, second, _, now, _ = stores
    await first.configure(KEY, enabled())
    with pytest.raises(ValueError, match="another account monitor"):
        await second.configure(OTHER_KEY, enabled())
    assert (await second.get(OTHER_KEY)).settings.enabled is False
    assert (await first.claim_due("worker", now[0]))["state"].account_key == KEY


async def test_competing_enable_requests_do_not_silently_disable_each_other(stores):
    first, second, _, _, _ = stores
    results = await asyncio.gather(
        first.configure(KEY, enabled()), second.configure(OTHER_KEY, enabled()),
        return_exceptions=True,
    )
    assert sum(isinstance(value, ValueError) for value in results) == 1
    states = await asyncio.gather(first.get(KEY), second.get(OTHER_KEY))
    assert sum(state.settings.enabled for state in states) == 1


async def test_two_workers_only_claim_one_native_source(stores):
    first, second, _, now, _ = stores
    await first.configure(KEY, enabled())
    results = await asyncio.gather(
        first.claim_due("worker-a", now[0]), second.claim_due("worker-b", now[0]),
    )
    assert sum(value is not None for value in results) == 1
    winner = next(value for value in results if value is not None)
    assert winner["revision"] == 1
    assert winner["fence"] == 1
    state = await second.get(KEY)
    assert state.status == "sampling"
    assert state.last_started_at == now[0]


async def test_finish_saves_snapshot_and_next_due_atomically(stores):
    first, second, accounts, now, _ = stores
    await first.configure(KEY, enabled())
    claim = await first.claim_due("worker", now[0])
    await accounts.upsert_account(account(label="User renamed"))
    now[0] += timedelta(seconds=12)
    captured = snapshot(observed_at=now[0])
    assert await first.finish(claim, account(), [captured]) is True
    assert await accounts.get_snapshot("sample") == captured
    assert (await accounts.get_account(KEY)).label == "User renamed"
    state = await second.get(KEY)
    assert state.status == "waiting"
    assert state.last_finished_at == now[0]
    assert state.next_run_at == now[0] + timedelta(milliseconds=INTERVAL_MS)
    assert await second.claim_due("worker-b", now[0]) is None


async def test_snapshot_conflict_rolls_back_partial_capture_and_state(stores):
    first, second, accounts, now, _ = stores
    await accounts.add_snapshot(snapshot("existing"))
    await first.configure(KEY, enabled())
    claim = await first.claim_due("worker", now[0])
    conflict = snapshot("existing").model_copy(update={"observed_at": START + timedelta(seconds=1)})
    with pytest.raises(ValueError, match="different content"):
        await first.finish(claim, account(), [snapshot("fresh"), conflict])
    assert await accounts.get_snapshot("fresh") is None
    assert (await second.get(KEY)).status == "sampling"
    assert await second.claim_due("other", now[0]) is None
    assert await first.release_claim(claim) is True


async def test_disable_rejects_old_capture_but_retains_source_until_cancel(stores):
    first, second, accounts, now, _ = stores
    await first.configure(KEY, enabled())
    old = await first.claim_due("old", now[0])
    disabled = await second.configure(KEY, PricingMonitorSettings())
    await second.configure(OTHER_KEY, enabled())
    assert disabled.revision == old["revision"] + 1
    assert await second.claim_due("new", now[0]) is None
    assert await first.finish(old, account(), [snapshot()]) is False
    assert await first.renew_claim(old, now[0]) is False
    assert await accounts.get_snapshot("sample") is None
    assert await first.release_claim(old) is True
    new = await second.claim_due("new", now[0])
    assert new["state"].account_key == OTHER_KEY
    assert (await first.get(KEY)).status == "disabled"


async def test_reenable_invalidates_old_generation_and_old_release_cannot_clear_new_lease(stores):
    first, second, accounts, now, _ = stores
    await first.configure(KEY, enabled())
    old = await first.claim_due("worker", now[0])
    await second.configure(KEY, PricingMonitorSettings())
    await second.configure(KEY, enabled())
    assert await first.finish(old, account(), [snapshot("old")]) is False
    assert await second.claim_due("worker", now[0]) is None
    assert await first.release_claim(old) is True
    new = await second.claim_due("worker", now[0])
    assert new["fence"] > old["fence"]
    assert await first.release_claim(old) is False
    assert await first.renew_claim(old, now[0]) is False
    assert await accounts.get_snapshot("old") is None


async def test_expired_worker_cannot_commit_and_restart_can_reclaim(stores):
    first, second, accounts, now, db_path = stores
    await first.configure(KEY, enabled())
    old = await first.claim_due("lost-worker", now[0])
    now[0] += timedelta(seconds=60)
    assert await first.finish(old, account(), [snapshot("stale")]) is False
    restarted = MonitorRepository(f"sqlite+aiosqlite:///{db_path}")
    fresh = await restarted.claim_due("restarted", now[0])
    assert fresh["fence"] > old["fence"]
    assert await second.release_claim(old) is False
    assert await restarted.finish(fresh, account(), [snapshot("fresh", observed_at=now[0])]) is True
    assert await accounts.get_snapshot("stale") is None
    assert await accounts.get_snapshot("fresh") is not None


async def test_renewal_extends_exclusion_and_expired_lease_cannot_renew(stores):
    first, second, _, now, _ = stores
    await first.configure(KEY, enabled())
    claim = await first.claim_due("worker", now[0])
    now[0] += timedelta(seconds=30)
    assert await first.renew_claim(claim, now[0]) is True
    now[0] += timedelta(seconds=31)
    assert await second.claim_due("other", now[0]) is None
    now[0] = START + timedelta(seconds=90)
    assert await first.renew_claim(claim, now[0]) is False
    assert await second.claim_due("other", now[0]) is not None


async def test_cancellation_releases_without_counting_as_failure(stores):
    first, second, accounts, now, _ = stores
    await first.configure(KEY, enabled())
    claim = await first.claim_due("worker", now[0])
    assert await first.release_claim(claim) is True
    state = await second.get(KEY)
    assert state.status == "waiting"
    assert state.last_error is None
    assert state.last_finished_at is None
    assert state.next_run_at == now[0] + timedelta(milliseconds=INTERVAL_MS)
    assert await accounts.list_snapshots(KEY) == []


async def test_error_delays_retry_from_finish_instead_of_old_due_time(stores):
    first, second, accounts, now, _ = stores
    await first.configure(KEY, enabled())
    claim = await first.claim_due("worker", now[0])
    now[0] += timedelta(seconds=20)
    assert await first.finish(claim, None, [], error="Native capture unavailable.") is True
    state = await second.get(KEY)
    assert state.status == "error"
    assert state.last_error == "Native capture unavailable."
    assert state.last_finished_at == now[0]
    assert state.next_run_at == now[0] + timedelta(milliseconds=INTERVAL_MS)
    assert await second.claim_due("worker-b", now[0]) is None
    assert await accounts.list_snapshots(KEY) == []


async def test_account_switch_pause_writes_no_other_account_data(stores):
    first, second, accounts, now, _ = stores
    await first.configure(KEY, enabled())
    claim = await first.claim_due("worker", now[0])
    assert await first.finish(
        claim, account(OTHER_KEY), [snapshot("other", OTHER_KEY)],
        error="The native account changed.", pause=True,
    ) is True
    state = await second.get(KEY)
    assert state.status == "paused_account_changed"
    assert state.settings.enabled is True
    assert state.next_run_at is None
    assert await accounts.get_snapshot("other") is None
    now[0] += timedelta(days=2)
    assert await second.claim_due("worker-b", now[0]) is None
    await second.configure(KEY, enabled())
    assert await second.claim_due("worker-b", now[0]) is not None


async def test_unmarked_account_switch_is_rejected_without_data(stores):
    first, second, accounts, now, _ = stores
    await first.configure(KEY, enabled())
    claim = await first.claim_due("worker", now[0])
    with pytest.raises(ValueError, match="account changed"):
        await first.finish(claim, account(OTHER_KEY), [snapshot("other", OTHER_KEY)])
    assert await accounts.get_snapshot("other") is None
    assert (await second.get(KEY)).status == "sampling"


async def test_restart_samples_once_and_does_not_replay_missed_periods(stores):
    first, second, _, now, _ = stores
    await first.configure(KEY, enabled())
    now[0] += timedelta(days=3)
    claim = await second.claim_due("restarted", now[0])
    assert claim is not None
    assert await second.finish(claim, account(), [snapshot(observed_at=now[0])]) is True
    assert await first.claim_due("worker-b", now[0]) is None
    assert (await first.get(KEY)).next_run_at == now[0] + timedelta(milliseconds=INTERVAL_MS)


async def test_manual_capture_and_monitor_share_source_lock_both_directions(stores):
    first, second, _, now, _ = stores
    await first.configure(KEY, enabled())
    manual = await first.claim_source("manual", now[0])
    assert manual is not None
    assert await second.claim_due("monitor", now[0]) is None
    assert await second.claim_source("manual-b", now[0]) is None
    assert await first.release_source(manual) is True
    monitor = await second.claim_due("monitor", now[0])
    assert monitor is not None
    assert await first.claim_source("manual", now[0]) is None
    assert await second.release_claim(monitor) is True
    assert await first.claim_source("manual", now[0]) is not None


async def test_competing_manual_and_due_claims_have_one_winner(stores):
    first, second, _, now, _ = stores
    await first.configure(KEY, enabled())
    claims = await asyncio.gather(
        first.claim_source("manual", now[0]), second.claim_due("monitor", now[0]),
    )
    assert sum(claim is not None for claim in claims) == 1


async def test_manual_source_expiry_fences_old_release(stores):
    first, second, _, now, _ = stores
    old = await first.claim_source("manual", now[0])
    now[0] += timedelta(seconds=60)
    fresh = await second.claim_source("manual", now[0])
    assert fresh["fence"] > old["fence"]
    assert await first.release_source(old) is False
    assert await second.release_source(fresh) is True


async def test_manual_capture_commits_only_before_source_lease_expires(stores):
    first, second, accounts, now, _ = stores
    stale = await first.claim_source("manual", now[0])
    now[0] += timedelta(seconds=60)
    assert await first.save_source_capture(stale, account(), [snapshot("expired")]) is None
    assert await accounts.get_snapshot("expired") is None
    fresh = await second.claim_source("manual-new", now[0])
    await accounts.upsert_account(account(label="User renamed"))
    captured = snapshot("fresh", observed_at=now[0])
    assert await second.save_source_capture(fresh, account(), [captured]) == (
        account(label="User renamed"), [captured],
    )
    assert await accounts.get_snapshot("fresh") == captured
    assert await second.release_source(fresh) is False
    assert await first.claim_source("next", now[0]) is not None


async def test_first_confirmed_capture_creates_only_that_accounts_default_plan(stores):
    first, second, accounts, now, _ = stores
    actual = "c" * 64
    claim = await first.claim_source("bootstrap", now[0])
    assert await first.save_source_capture(claim, account(actual), [snapshot("current", actual)])
    state = await second.get(actual)
    assert state.settings == PricingMonitorSettings(enabled=True, interval_ms=1800000)
    assert state.revision == 1 and state.status == "waiting"
    assert state.last_finished_at == now[0]
    assert state.next_run_at == now[0] + timedelta(minutes=30)
    assert state.runtime_running is False
    assert not (await second.get(KEY)).settings.enabled
    assert not (await second.get(OTHER_KEY)).settings.enabled
    assert await accounts.get_snapshot("current") is not None
    assert await second.claim_due("not-yet", now[0]) is None
    now[0] += timedelta(minutes=30)
    assert (await second.claim_due("scheduled", now[0]))["state"].account_key == actual


@pytest.mark.parametrize("paused", [False, True])
@pytest.mark.parametrize("interval_ms", [300000, 86400000])
async def test_source_capture_preserves_existing_settings_and_revision(stores, paused, interval_ms):
    first, second, _, now, _ = stores
    await store_legacy_interval(first, interval_ms)
    if paused:
        await first.configure(KEY, PricingMonitorSettings(enabled=False))
    before = await second.get(KEY)
    for name in ("first", "restarted"):
        now[0] += timedelta(seconds=10)
        claim = await second.claim_source(name, now[0])
        assert await second.save_source_capture(claim, account(), [snapshot(name)])
        expected = before if paused else before.model_copy(update={"last_finished_at": now[0]})
        assert await first.get(KEY) == expected


@pytest.mark.parametrize("target_paused", [False, True])
async def test_confirmed_account_switch_stops_old_monitor_without_overriding_pause(stores, target_paused):
    first, second, accounts, now, _ = stores
    previous = await first.configure(KEY, enabled())
    paused = await first.configure(OTHER_KEY, PricingMonitorSettings(enabled=False)) if target_paused else None
    claim = await second.claim_source("switched-native-source", now[0])
    assert await second.save_source_capture(claim, account(OTHER_KEY), [snapshot("new-account", OTHER_KEY)])
    old, current = await first.get(KEY), await first.get(OTHER_KEY)
    assert not old.settings.enabled and old.status == "paused_account_changed"
    assert old.settings.interval_ms == previous.settings.interval_ms
    assert old.next_run_at is None and old.revision == previous.revision + 1
    assert current == paused if target_paused else current.settings.enabled
    assert await accounts.list_snapshots(KEY) == []
    assert len(await accounts.list_snapshots(OTHER_KEY)) == 1
    now[0] += timedelta(hours=1)
    due = await first.claim_due("after-switch", now[0])
    assert due is None if target_paused else due["state"].account_key == OTHER_KEY


@pytest.mark.parametrize("invalid", ["account", "snapshot", "mismatch"])
async def test_invalid_native_capture_cannot_create_default_settings(stores, invalid):
    first, second, accounts, now, _ = stores
    native_account, captured = account(), snapshot()
    if invalid == "account":
        native_account = native_account.model_copy(update={"account_key": "bad-key"})
    elif invalid == "snapshot":
        captured = captured.model_copy(update={"used_percent": Decimal(101)})
    else:
        captured = snapshot(key=OTHER_KEY)
    claim = await first.claim_source("invalid-capture", now[0])
    with pytest.raises(ValueError):
        await first.save_source_capture(claim, native_account, [captured])
    assert not (await second.get(KEY)).settings.enabled
    assert (await second.get(KEY)).revision == 0
    assert await accounts.list_snapshots(KEY) == []
    await first.release_source(claim)


async def test_expired_bootstrap_rolls_back_default_on_and_account_switch(stores, monkeypatch):
    first, second, accounts, now, _ = stores
    original = first._default_on_captured_account
    before = await first.configure(KEY, enabled())

    async def expire_after_default(*args, **kwargs):
        await original(*args, **kwargs)
        now[0] += timedelta(seconds=61)

    monkeypatch.setattr(first, "_default_on_captured_account", expire_after_default)
    claim = await first.claim_source("expiring-bootstrap", now[0])
    assert await first.save_source_capture(claim, account(OTHER_KEY), [snapshot("new", OTHER_KEY)]) is None
    assert await second.get(KEY) == before
    assert (await second.get(OTHER_KEY)).revision == 0
    assert await accounts.get_snapshot("new") is None


async def test_source_renewal_cannot_revive_expired_or_replaced_bootstrap(stores):
    first, second, _, now, _ = stores
    old = await first.claim_source("bootstrap", now[0])
    now[0] += timedelta(seconds=10)
    assert await second.renew_source(old, now[0]) is True
    now[0] += timedelta(seconds=60)
    assert await first.renew_source(old, now[0]) is False
    fresh = await second.claim_source("bootstrap", now[0])
    assert fresh is not None
    assert await first.renew_source(old, now[0]) is False
    assert await first.save_source_capture(old, account(), [snapshot()]) is None
    assert (await second.get(KEY)).revision == 0
    await second.release_source(fresh)


async def test_empty_capture_cannot_be_recorded_as_success(stores):
    first, second, _, now, _ = stores
    await first.configure(KEY, enabled())
    claim = await first.claim_due("worker", now[0])
    with pytest.raises(ValueError, match="requires an account and snapshots"):
        await first.finish(claim, account(), [])
    assert (await second.get(KEY)).last_finished_at is None


async def test_lease_expiring_during_snapshot_write_rolls_back_everything(stores, monkeypatch):
    first, second, accounts, now, _ = stores
    original_add = AccountUsageRepository._add_snapshot

    async def expire_after_insert(session, captured):
        result = await original_add(session, captured)
        now[0] += timedelta(seconds=61)
        return result

    monkeypatch.setattr(AccountUsageRepository, "_add_snapshot", expire_after_insert)
    await first.configure(KEY, enabled())
    claim = await first.claim_due("worker", now[0])
    assert await first.finish(claim, account(), [snapshot()]) is False
    assert await accounts.get_snapshot("sample") is None
    assert (await second.get(KEY)).last_finished_at is None


def plan_snapshot(captured=None):
    captured = captured or snapshot()
    return PlanUsageSnapshot(
        snapshot_id=captured.snapshot_id, account_key=captured.account_key,
        limit_id=captured.limit_id, used_percent=int(captured.used_percent),
        window_duration_ms=captured.window_duration_ms,
        resets_at=captured.resets_at, observed_at=captured.observed_at,
        activity_observed_at=captured.observed_at, activity_tokens=123456,
        activity_scope="c" * 64, source="codex_account_activity",
    )


async def test_monitor_finish_saves_matching_plan_records(stores):
    first, second, accounts, now, _ = stores
    await first.configure(KEY, enabled())
    claim = await first.claim_due("worker", now[0])
    plan = plan_snapshot()
    assert await first.finish(claim, account(), [snapshot()], plan_snapshots=[plan]) is True
    assert await accounts.list_plan_snapshots(KEY) == [plan]
    assert (await second.get(KEY)).status == "waiting"


async def test_manual_capture_saves_matching_plan_records(stores):
    first, _, accounts, now, _ = stores
    claim = await first.claim_source("manual", now[0])
    captured, plan = snapshot(), plan_snapshot()
    result = await first.save_source_capture(claim, account(), [captured], plan_snapshots=[plan])
    assert result == (account(), [captured])
    assert await accounts.list_plan_snapshots(KEY) == [plan]


@pytest.mark.parametrize("manual", [False, True])
async def test_expired_claim_saves_neither_quota_nor_plan(stores, manual):
    first, _, accounts, now, _ = stores
    await first.configure(KEY, enabled())
    claim = await (
        first.claim_source("manual", now[0]) if manual else first.claim_due("worker", now[0])
    )
    now[0] += timedelta(seconds=60)
    if manual:
        result = await first.save_source_capture(claim, account(), [snapshot()], plan_snapshots=[plan_snapshot()])
        assert result is None
    else:
        assert await first.finish(claim, account(), [snapshot()], plan_snapshots=[plan_snapshot()]) is False
    assert await accounts.get_snapshot("sample") is None
    assert await accounts.list_plan_snapshots(KEY) == []


@pytest.mark.parametrize("manual", [False, True])
async def test_mixed_account_plan_cannot_cross_capture_transaction(stores, manual):
    first, second, accounts, now, _ = stores
    await first.configure(KEY, enabled())
    claim = await (
        first.claim_source("manual", now[0]) if manual else first.claim_due("worker", now[0])
    )
    foreign_plan = plan_snapshot(snapshot(key=OTHER_KEY))
    with pytest.raises(ValueError, match="captured account"):
        if manual:
            await first.save_source_capture(claim, account(), [snapshot()], plan_snapshots=[foreign_plan])
        else:
            await first.finish(claim, account(), [snapshot()], plan_snapshots=[foreign_plan])
    assert await accounts.get_snapshot("sample") is None
    assert await accounts.list_plan_snapshots(KEY) == []
    assert await accounts.list_plan_snapshots(OTHER_KEY) == []
    if not manual:
        assert (await second.get(KEY)).last_finished_at is None


@pytest.mark.parametrize("manual", [False, True])
async def test_lease_expiring_after_plan_write_rolls_back_all_capture_data(stores, monkeypatch, manual):
    first, second, accounts, now, _ = stores
    original_add = AccountUsageRepository._add_plan_snapshot

    async def expire_after_plan_insert(session, captured):
        result = await original_add(session, captured)
        now[0] += timedelta(seconds=61)
        return result

    monkeypatch.setattr(AccountUsageRepository, "_add_plan_snapshot", expire_after_plan_insert)
    await first.configure(KEY, enabled())
    claim = await (
        first.claim_source("manual", now[0]) if manual else first.claim_due("worker", now[0])
    )
    if manual:
        result = await first.save_source_capture(claim, account(), [snapshot()], plan_snapshots=[plan_snapshot()])
        assert result is None
    else:
        assert await first.finish(claim, account(), [snapshot()], plan_snapshots=[plan_snapshot()]) is False
    assert await accounts.get_snapshot("sample") is None
    assert await accounts.list_plan_snapshots(KEY) == []
    assert (await second.get(KEY)).last_finished_at is None


async def test_returning_account_restores_schedule_and_preserves_history(stores):
    first, second, accounts, now, _ = stores
    await accounts.upsert_account(account(label="Custom label"))
    await first.configure(KEY, enabled())
    for identifier, key in (("a-before", KEY), ("b", OTHER_KEY), ("a-return", KEY)):
        now[0] += timedelta(seconds=10)
        claim = await first.claim_source(identifier, now[0])
        assert await first.save_source_capture(claim, account(key), [snapshot(identifier, key, now[0])])
    current = await second.get(KEY)
    assert current.settings.enabled and current.status == "waiting"
    assert current.settings.interval_ms == INTERVAL_MS
    assert current.next_run_at == now[0] + timedelta(milliseconds=INTERVAL_MS)
    assert current.last_error is None
    assert (await second.get(OTHER_KEY)).status == "paused_account_changed"
    assert (await accounts.get_account(KEY)).label == "Custom label"
    assert {row.snapshot_id for row in await accounts.list_snapshots(KEY)} == {"a-before", "a-return"}
    assert {row.snapshot_id for row in await accounts.list_snapshots(OTHER_KEY)} == {"b"}


async def test_relogin_recovers_auth_pause_but_explicit_disable_during_capture_wins(stores):
    first, second, accounts, now, _ = stores
    await first.configure(KEY, enabled())
    due = await first.claim_due("logout", now[0])
    assert await first.finish(due, None, [], error="not logged in", pause=True)
    claim = await first.claim_source("relogin", now[0])
    assert await first.save_source_capture(claim, account(), [snapshot("relogin")])
    resumed = await second.get(KEY)
    assert resumed.status == "waiting" and resumed.settings.enabled
    claim = await first.claim_source("in-flight", now[0])
    disabled = await second.configure(KEY, PricingMonitorSettings(enabled=False))
    assert await first.save_source_capture(claim, account(), [snapshot("after-disable")])
    assert await second.get(KEY) == disabled
    assert len(await accounts.list_snapshots(KEY)) == 2


async def test_successful_source_capture_clears_error_without_postponing_due_sample(stores):
    first, second, accounts, now, _ = stores
    await first.configure(KEY, enabled())
    due = await first.claim_due("failed-round", now[0])
    assert await first.finish(due, None, [], error="temporary network failure")
    failed = await second.get(KEY)
    now[0] += timedelta(seconds=10)
    source = await second.claim_source("successful-relogin", now[0])
    assert await second.save_source_capture(source, account(), [snapshot("recovered", observed_at=now[0])])
    recovered = await first.get(KEY)
    assert recovered.status == "waiting" and recovered.last_error is None
    assert recovered.last_finished_at == now[0] != failed.last_finished_at
    assert recovered.settings == failed.settings
    assert recovered.revision == failed.revision
    assert recovered.next_run_at == failed.next_run_at
    assert await accounts.get_snapshot("recovered") is not None
