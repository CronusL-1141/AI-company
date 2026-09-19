"""Dollar samples and quota evidence share persistence and lease boundaries."""

import asyncio
import sqlite3
from datetime import timedelta
from decimal import Decimal

import pytest

from aiteam.services.account_monitor import AccountMonitorRunner
from aiteam.services.plan_pricing import estimate_pricing_plan_capacity
from aiteam.storage.account_monitor import MonitorRepository
from aiteam.storage.account_usage import AccountUsageRepository
from aiteam.storage.engine_pool import engine_pool
from aiteam.types import PlanUsageSnapshot, PricingMonitorSettings

from .._plan_pricing_fixtures import BASE, KEY, account, catalog, entry, price_snapshot, quota


@pytest.fixture
async def stores(tmp_path, monkeypatch):
    location = tmp_path / "pricing.sqlite"
    url = f"sqlite+aiosqlite:///{location}"
    first, second = AccountUsageRepository(url), AccountUsageRepository(url)
    monitor = MonitorRepository(url)
    await first.init_db()
    await monitor.init_db()
    clock = [BASE]
    monkeypatch.setattr("aiteam.storage.account_monitor.utc_now", lambda: clock[0])
    yield first, second, monitor, clock, location
    await engine_pool.get_engine(url).dispose()


async def save(repository, value):
    return await repository.save_capture(account(value.account_key), [quota(value)], pricing_plan_snapshots=[value])


async def test_price_capture_roundtrips_without_mutating_old_token_snapshot(stores):
    first, second, _, _, location = stores
    baseline = price_snapshot()
    old = PlanUsageSnapshot(
        **{field: getattr(baseline, field) for field in (
            "snapshot_id", "account_key", "limit_id", "window_duration_ms", "resets_at", "observed_at", "used_percent",
        )},
        activity_tokens=123, activity_scope="c" * 64, activity_observed_at=BASE,
        source="codex_account_activity",
    )
    await first.save_capture(account(), [quota(baseline)], plan_snapshots=[old])
    with sqlite3.connect(f"file:{location}?mode=ro", uri=True) as database:
        before = database.execute("SELECT payload FROM account_plan_snapshots").fetchall()
    await save(first, baseline)
    latest = price_snapshot("end", at=BASE + timedelta(seconds=1), percent=21, entries=[entry()])
    await save(second, latest)
    prices = await first.list_plan_price_snapshots(KEY)
    assert prices == [baseline, latest]
    assert estimate_pricing_plan_capacity(prices, now=latest.observed_at)[0].estimated_total_usd == Decimal(".294")
    with sqlite3.connect(f"file:{location}?mode=ro", uri=True) as database:
        assert database.execute("SELECT payload FROM account_plan_snapshots").fetchall() == before
    assert await second.list_plan_snapshots(KEY) == [old]


async def test_repeated_price_capture_is_idempotent_and_conflicts_are_atomic(stores):
    first, second, _, _, _ = stores
    baseline = price_snapshot()
    assert await asyncio.gather(save(first, baseline), save(second, baseline)) == [
        (account(), [quota(baseline)]), (account(), [quota(baseline)]),
    ]
    changed = baseline.model_copy(update={"activity_usd": Decimal("1")})
    with pytest.raises(ValueError, match="different content"):
        await save(second, changed)
    assert await first.list_plan_price_snapshots(KEY) == [baseline]


async def test_cumulative_money_must_equal_verified_quotes(stores):
    first, second, _, _, _ = stores
    baseline = price_snapshot()
    await save(first, baseline)
    latest = price_snapshot("end", at=BASE + timedelta(seconds=1), percent=21, entries=[entry()])
    invalid = latest.model_copy(update={"activity_usd": Decimal("42")})
    with pytest.raises(ValueError, match="must equal previous"):
        await save(first, invalid)
    assert await second.get_snapshot("end") is None
    assert await second.list_plan_price_snapshots(KEY) == [baseline]


async def test_prediction_partial_cumulative_roundtrips_without_claiming_complete_activity(stores):
    first, second, _, _, _ = stores
    baseline = price_snapshot().model_copy(update={"prediction_activity_usd": Decimal(0)})
    await save(first, baseline)
    at = BASE + timedelta(seconds=1)
    partial = price_snapshot("partial", at=at, percent=21, complete=False,
                             entries=[entry(), entry("unknown", model="unknown")])
    partial = partial.model_copy(update={"prediction_activity_usd": Decimal(".00294")})
    await save(first, partial)
    latest = price_snapshot("later", at=at + timedelta(seconds=1), start=at, percent=22,
                            entries=[entry("next", at=at + timedelta(seconds=1))])
    latest = latest.model_copy(update={"activity_usd": None, "prediction_activity_usd": Decimal(".00588")})
    await save(first, latest)
    values = await second.list_plan_price_snapshots(KEY)
    assert values[-1].activity_usd is None and values[-1].pricing.complete
    assert values[-2].activity_usd is None and not values[-2].pricing.complete
    result = estimate_pricing_plan_capacity(values, now=latest.observed_at)[0]
    assert result.start_snapshot_id == baseline.snapshot_id and result.estimated_total_usd == Decimal(".294")


async def test_prediction_cumulative_rejects_forged_subtotal_atomically(stores):
    first, second, _, _, _ = stores
    baseline = price_snapshot()
    await save(first, baseline)
    latest = price_snapshot("end", at=BASE + timedelta(seconds=1), percent=21, complete=False, entries=[entry()])
    invalid = latest.model_copy(update={"prediction_activity_usd": Decimal("42")})
    with pytest.raises(ValueError, match="known prices from the cycle anchor"):
        await save(first, invalid)
    assert await second.get_snapshot("end") is None
    assert await second.list_plan_price_snapshots(KEY) == [baseline]


async def test_catalog_switch_cannot_continue_old_cumulative_dollars(stores):
    first, second, _, _, _ = stores
    await save(first, price_snapshot())
    at = BASE + timedelta(seconds=1)
    await save(first, price_snapshot("first", at=at, percent=21, entries=[entry()]))
    later = price_snapshot("later", at=at + timedelta(seconds=1), start=at, percent=22,
                            entries=[entry("next", at=at + timedelta(seconds=1))], price_catalog=catalog("v2"))
    with pytest.raises(ValueError, match="matching previous"):
        await save(second, later)
    assert await first.get_snapshot("later") is None


async def test_mixed_account_or_window_price_snapshot_rolls_back_all_data(stores):
    first, second, _, _, _ = stores
    baseline = price_snapshot()
    foreign = price_snapshot(key="d" * 64)
    with pytest.raises(ValueError, match="account capture"):
        await first.save_capture(account(), [quota(baseline)], pricing_plan_snapshots=[foreign])
    assert await second.list_accounts() == []
    wrong = baseline.model_copy(update={"used_percent": 21})
    with pytest.raises(ValueError, match="must match the quota"):
        await first.save_capture(account(), [quota(baseline)], pricing_plan_snapshots=[wrong])
    assert await second.list_accounts() == []
    assert await second.list_plan_price_snapshots(KEY) == []


@pytest.mark.parametrize("manual", [True, False])
async def test_expired_fence_discards_price_and_quota(stores, manual):
    first, second, monitor, clock, _ = stores
    await first.upsert_account(account())
    await monitor.configure(KEY, PricingMonitorSettings(enabled=True))
    claim = await (monitor.claim_source("manual", clock[0]) if manual else monitor.claim_due("worker", clock[0]))
    clock[0] += timedelta(seconds=60)
    value = price_snapshot()
    if manual:
        assert await monitor.save_source_capture(
            claim, account(), [quota(value)], pricing_plan_snapshots=[value],
        ) is None
    else:
        assert await monitor.finish(claim, account(), [quota(value)], pricing_plan_snapshots=[value]) is False
    assert await second.get_snapshot(value.snapshot_id) is None
    assert await second.list_plan_price_snapshots(KEY) == []


@pytest.mark.parametrize("manual", [True, False])
async def test_lease_expiring_after_monetary_insert_rolls_back_everything(stores, monkeypatch, manual):
    first, second, monitor, clock, _ = stores
    await first.upsert_account(account())
    await monitor.configure(KEY, PricingMonitorSettings(enabled=True))
    claim = await (monitor.claim_source("manual", clock[0]) if manual else monitor.claim_due("worker", clock[0]))
    original = AccountUsageRepository._add_pricing_plan_snapshot

    async def expired_after_write(session, snapshot):
        result = await original(session, snapshot)
        clock[0] += timedelta(seconds=61)
        return result

    monkeypatch.setattr(AccountUsageRepository, "_add_pricing_plan_snapshot", expired_after_write)
    value = price_snapshot()
    if manual:
        assert await monitor.save_source_capture(
            claim, account(), [quota(value)], pricing_plan_snapshots=[value],
        ) is None
    else:
        assert await monitor.finish(claim, account(), [quota(value)], pricing_plan_snapshots=[value]) is False
    assert await second.get_snapshot(value.snapshot_id) is None
    assert await second.list_plan_price_snapshots(KEY) == []
    assert (await monitor.get(KEY)).last_finished_at is None


async def test_runner_persists_four_item_capture_result(stores):
    first, second, monitor, clock, _ = stores
    await first.upsert_account(account())
    await monitor.configure(KEY, PricingMonitorSettings(enabled=True))
    value = price_snapshot()

    async def capture():
        return account(), [quota(value)], [], [value]

    runner = AccountMonitorRunner(monitor, capture=capture, clock=lambda: clock[0])
    assert await runner.tick() is True
    assert await second.list_plan_price_snapshots(KEY) == [value]
    assert (await monitor.get(KEY)).last_finished_at == BASE
