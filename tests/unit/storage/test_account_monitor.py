"""Fenced account monitoring must survive workers, cancellation, and restarts."""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import timedelta

import pytest

from aiteam.clock import from_timestamp
from aiteam.storage.account_monitor import MonitorRepository
from aiteam.storage.account_usage import AccountUsageRepository
from aiteam.storage.engine_pool import engine_pool
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


@pytest.mark.parametrize("interval_ms", [299999, 86400001, "300000", 300000.0, True])
def test_interval_requires_bounded_integer_milliseconds(interval_ms):
    with pytest.raises(ValueError):
        PricingMonitorSettings(interval_ms=interval_ms)


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
