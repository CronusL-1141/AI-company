"""Manual boundaries survive persistence without replacing sampled evidence."""

import asyncio
import sqlite3
from contextlib import asynccontextmanager
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

from aiteam.api.routes import account_usage as routes
from aiteam.services.plan_pricing import estimate_pricing_plan_capacity, prediction_cycle_total
from aiteam.storage import account_usage as storage
from aiteam.storage.account_monitor import MonitorRepository
from aiteam.storage.account_usage import AccountUsageRepository
from aiteam.storage.engine_pool import engine_pool
from aiteam.types import PricingPlanAnchorReset

from ._plan_pricing_fixtures import BASE, KEY, account, entry, price_snapshot, quota

WINDOW = 604800000
REQUEST = PricingPlanAnchorReset(limit_id="codex", window_duration_ms=WINDOW)
RESET_URL = f"/api/account-usage/{KEY}/plan-anchor/reset"


def next_point(previous, identifier, *, percent=None, key=KEY, **changes):
    at = previous.observed_at + timedelta(seconds=1)
    point = price_snapshot(
        identifier, at=at, start=previous.observed_at,
        previous_usd=previous.activity_usd or Decimal(0), key=key,
        percent=previous.used_percent + 1 if percent is None else percent,
        entries=[entry(identifier, at=at)],
    )
    return point.model_copy(update=changes)


async def save(repository, point):
    await repository.save_capture(account(point.account_key), [quota(point)], pricing_plan_snapshots=[point])


def raw_evidence(location):
    with sqlite3.connect(f"file:{location}?mode=ro", uri=True) as database:
        tables = [row[0] for row in database.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name != 'account_plan_price_anchors'",
        )]
        return {table: database.execute(f'SELECT * FROM "{table}" ORDER BY 1').fetchall() for table in tables}


@pytest.fixture
def client(tmp_path, monkeypatch):
    location = tmp_path / "anchors.sqlite"
    repository = AccountUsageRepository(f"sqlite+aiosqlite:///{location}")
    state = SimpleNamespace(now=BASE + timedelta(seconds=10))
    monkeypatch.setattr(storage, "utc_now", lambda: state.now)
    monkeypatch.setattr(routes, "utc_now", lambda: state.now)

    async def forbidden_capture(**kwargs):
        raise AssertionError("reset and GET must not capture native usage")

    monkeypatch.setattr(routes, "capture_account", forbidden_capture)

    @asynccontextmanager
    async def lifespan(app):
        await repository.init_db()
        await MonitorRepository(repository._db_url).init_db()
        yield
        await engine_pool.get_engine(repository._db_url).dispose()

    app = FastAPI(lifespan=lifespan)
    app.include_router(routes.router)
    app.dependency_overrides[routes.get_account_repository] = lambda: repository
    with TestClient(app) as connected:
        yield connected, repository, state, location


def get_estimate(connected, key=KEY):
    result = connected.get(f"/api/account-usage/{key}", params={"include_pricing": "false"})
    assert result.status_code == 200, result.text
    return result.json()["data"]["pricing_plan_estimates"]


def reset(connected, **changes):
    return connected.post(RESET_URL, json={"limit_id": "codex", "window_duration_ms": WINDOW, **changes})


def test_reset_roundtrips_clears_previous_estimate_and_preserves_all_evidence(client):
    connected, repository, state, location = client
    first = price_snapshot()
    latest = next_point(first, "latest", percent=25)
    latest = latest.model_copy(update={"prediction_activity_usd": prediction_cycle_total([first, latest])})
    for point in [first, latest]:
        connected.portal.call(save, repository, point)
    original = get_estimate(connected)[0]
    assert original["status"] == "estimated"
    before = raw_evidence(location)
    response = reset(connected)
    assert response.status_code == 200, response.text
    anchor = response.json()["data"]
    assert anchor["snapshot_id"] == latest.snapshot_id and anchor["revision"] == 1
    assert anchor["used_percent"] == 25
    assert anchor["observed_at"] == latest.observed_at.isoformat().replace("+00:00", "Z")
    state.now += timedelta(seconds=1)
    assert reset(connected).json()["data"] == anchor
    connected.portal.call(engine_pool.get_engine(repository._db_url).dispose)
    reopened = AccountUsageRepository(repository._db_url)
    connected.app.dependency_overrides[routes.get_account_repository] = lambda: reopened
    result = get_estimate(connected)[0]
    assert result["start_snapshot_id"] == latest.snapshot_id
    assert result["status"] == "collecting" and result["delta_used_percent"] == 0
    assert Decimal(result["delta_usd"]) == 0
    assert result["estimated_total_usd"] is None and result["last_estimated_total_usd"] is None
    assert result["last_estimate_observed_at"] is None
    assert raw_evidence(location) == before

    next_sample = next_point(latest, "next", percent=26)
    next_sample = next_sample.model_copy(update={
        "prediction_activity_usd": prediction_cycle_total([first, latest, next_sample]),
    })
    connected.portal.call(save, reopened, next_sample)
    result = get_estimate(connected)[0]
    assert result["start_snapshot_id"] == latest.snapshot_id and result["delta_used_percent"] == 1
    assert Decimal(result["delta_usd"]) == Decimal(".00294")
    assert Decimal(result["estimated_total_usd"]) == Decimal(".294")
    saved = connected.portal.call(reopened.list_plan_price_snapshots, KEY)
    assert saved[-1].prediction_activity_usd == Decimal(".00588")
    second = reset(connected).json()["data"]
    assert second["revision"] == 2 and second["snapshot_id"] == next_sample.snapshot_id


@pytest.mark.parametrize("change", ["percent_drop", "reset_metadata"])
def test_only_usage_drop_invalidates_manual_anchor_after_percentage_rebounds(client, change):
    connected, repository, _, location = client
    first = price_snapshot()
    manual = next_point(first, "manual", percent=25)
    for point in [first, manual]:
        connected.portal.call(save, repository, point)
    assert reset(connected).status_code == 200
    natural = next_point(manual, "natural", percent=5 if change == "percent_drop" else 26)
    if change == "reset_metadata":
        natural = natural.model_copy(update={"resets_at": manual.resets_at + timedelta(days=1)})
    later = next_point(natural, "rebound", percent=30, resets_at=natural.resets_at)
    for point in [natural, later]:
        connected.portal.call(save, repository, point)
    before = raw_evidence(location)
    result = get_estimate(connected)[0]
    expected_anchor = natural if change == "percent_drop" else manual
    assert result["start_snapshot_id"] == expected_anchor.snapshot_id
    assert Decimal(result["delta_usd"]) == Decimal(".00294" if change == "percent_drop" else ".00588")
    assert result["delta_used_percent"] == later.used_percent - expected_anchor.used_percent
    assert raw_evidence(location) == before
    assert connected.portal.call(repository.list_plan_anchors, KEY)[0].snapshot_id == manual.snapshot_id


def test_reset_selects_latest_paired_main_window_and_is_account_isolated(client):
    connected, repository, _, _ = client
    first = price_snapshot()
    main = next_point(first, "main")
    spark = price_snapshot("spark", at=main.observed_at + timedelta(seconds=1), limit_id="spark", unavailable=True)
    other = price_snapshot("other", key="c" * 64)
    other_window = price_snapshot("short-window").model_copy(update={
        "window_duration_ms": 18000000, "resets_at": BASE + timedelta(hours=1),
    })
    for point in [first, main, spark, other, other_window]:
        connected.portal.call(save, repository, point)
    quota_only = quota(next_point(spark, "quota-only"))
    connected.portal.call(repository.add_snapshot, quota_only)
    before = get_estimate(connected, "c" * 64)
    response = reset(connected)
    assert response.status_code == 200, response.text
    assert response.json()["data"]["snapshot_id"] == "main"
    assert get_estimate(connected, "c" * 64) == before
    assert connected.portal.call(repository.list_plan_anchors, "c" * 64) == []
    estimates = get_estimate(connected)
    short = next(item for item in estimates if item["window_duration_ms"] == 18000000)
    assert short["start_snapshot_id"] == "short-window"


def test_late_prices_before_manual_boundary_stay_in_cycle_evidence_but_not_new_estimate(client):
    connected, repository, _, _ = client
    first = price_snapshot()
    manual = next_point(first, "manual", percent=25)
    for point in [first, manual]:
        connected.portal.call(save, repository, point)
    assert reset(connected).status_code == 200
    at = manual.observed_at + timedelta(seconds=1)
    later = price_snapshot(
        "late-quotes", at=at, start=BASE, percent=26, complete=False,
        entries=[entry("late-before", at=BASE + timedelta(milliseconds=500)),
                 entry("late-at", at=manual.observed_at), entry("new", at=at)],
    )
    later = later.model_copy(update={
        "prediction_activity_usd": prediction_cycle_total([first, manual, later]),
    })
    connected.portal.call(save, repository, later)
    result = get_estimate(connected)[0]
    assert result["start_snapshot_id"] == manual.snapshot_id and result["delta_used_percent"] == 1
    assert Decimal(result["delta_usd"]) == Decimal(".00294")
    assert Decimal(result["estimated_total_usd"]) == Decimal(".294")
    saved = connected.portal.call(repository.list_plan_price_snapshots, KEY)
    assert saved[-1].prediction_activity_usd == Decimal(".01176")
    assert saved[-1].activity_usd is None and not saved[-1].pricing.complete


@pytest.mark.parametrize("body", [
    {"limit_id": "spark", "window_duration_ms": WINDOW},
    {"limit_id": "codex", "window_duration_ms": "604800000"},
    {"limit_id": "codex", "window_duration_ms": True},
    {"limit_id": "codex", "window_duration_ms": 0},
    {"limit_id": "codex", "window_duration_ms": WINDOW, "snapshot_id": "chosen"},
    {"limit_id": "codex"},
])
def test_reset_body_is_strict_and_never_accepts_a_client_chosen_snapshot(client, body):
    connected, _, _, _ = client
    assert connected.post(RESET_URL, json=body).status_code == 422


def test_reset_missing_account_missing_pair_and_expired_window(client):
    connected, repository, state, _ = client
    assert reset(connected).status_code == 404
    connected.portal.call(repository.upsert_account, account())
    assert reset(connected).status_code == 409
    connected.portal.call(repository.add_snapshot, quota(price_snapshot("quota-only")))
    assert reset(connected).status_code == 409
    point = price_snapshot()
    connected.portal.call(save, repository, point)
    assert reset(connected, window_duration_ms=18000000).status_code == 409
    state.now = point.resets_at
    assert reset(connected).status_code == 409
    assert connected.portal.call(repository.list_plan_anchors, KEY) == []


async def test_reset_waits_for_the_sampling_transaction_then_chooses_its_latest_point(tmp_path, monkeypatch):
    url = f"sqlite+aiosqlite:///{tmp_path / 'concurrent.sqlite'}"
    first, second = AccountUsageRepository(url), AccountUsageRepository(url)
    await first.init_db()
    baseline = price_snapshot()
    await save(first, baseline)
    latest = next_point(baseline, "committing")
    monkeypatch.setattr(storage, "utc_now", lambda: latest.observed_at)
    reset_started = asyncio.Event()

    async def reset_after_start():
        reset_started.set()
        return await second.reset_plan_anchor(KEY, REQUEST)

    try:
        async with first._write_session() as session:
            await first._add_snapshot(session, quota(latest))
            await first._add_pricing_plan_snapshot(session, latest)
            pending = asyncio.create_task(reset_after_start())
            await reset_started.wait()
            await asyncio.sleep(0.03)
            assert not pending.done()
        anchor = await pending
        assert anchor.snapshot_id == latest.snapshot_id
        repeated = await asyncio.gather(
            first.reset_plan_anchor(KEY, REQUEST), second.reset_plan_anchor(KEY, REQUEST),
        )
        assert repeated == [anchor, anchor]
        assert (await second.list_plan_anchors(KEY)) == [anchor]
    finally:
        await engine_pool.get_engine(url).dispose()


async def test_old_database_without_anchor_table_is_read_compatible(tmp_path):
    url = f"sqlite+aiosqlite:///{tmp_path / 'legacy.sqlite'}"
    repository = AccountUsageRepository(url)
    await repository.init_db()
    baseline = price_snapshot()
    await save(repository, baseline)
    try:
        async with engine_pool.get_engine(url).begin() as connection:
            await connection.execute(text("DROP TABLE account_plan_price_anchors"))
        anchors = await repository.list_plan_anchors(KEY)
        assert anchors == []
        snapshots = await repository.list_plan_price_snapshots(KEY)
        result = estimate_pricing_plan_capacity(snapshots, now=BASE, anchors=anchors)[0]
        assert result.status == "collecting" and result.start_snapshot_id == baseline.snapshot_id
        async with engine_pool.get_engine(url).connect() as connection:
            assert await connection.scalar(text(
                "SELECT count(*) FROM sqlite_master WHERE name='account_plan_price_anchors'",
            )) == 0
    finally:
        await engine_pool.get_engine(url).dispose()
