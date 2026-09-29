"""Regression: account usage reads must not freeze the API as history grows.

2026-09-29 incident (task 551aee38). The Codex account monitor ran every 30 s and
each round read the account's whole plan and price history (~18K rows, 36 MB of
JSON) three times on the event loop to find one latest row or one running total;
the account page's detail endpoint did the same on every refresh. Rounds blocked the
loop 2-19 s (40-65% GC); /api/health and hook POSTs stalled 10-47 s under load.

Harness: a temp SQLite file seeded with a history large enough that one full read
takes a good fraction of a second, and an in-process loop-lag probe (an asyncio task
on the same loop) around the account page's detail endpoint, served in process over
ASGI, and around one monitor round's repository work. The probe measures what the
code under test does to the loop, not how an HTTP client gets scheduled on a busy
machine, and the bounds are relative to the measured cost of one full read, so they
hold both on a fast idle machine and on a loaded runner.
"""

from __future__ import annotations

import asyncio
import copy
import json
import sqlite3
import time
from datetime import timedelta
from decimal import Decimal

import httpx
import pytest
from fastapi import FastAPI

from aiteam.api.routes import account_usage as routes
from aiteam.services import local_plan_capture
from aiteam.services.plan_summary import order_key
from aiteam.storage.account_usage import AccountUsageRepository
from aiteam.storage.engine_pool import engine_pool
from aiteam.types import PlanUsageSnapshot, PricingPlanSnapshot

from .._plan_history import WEEK, history_catalog
from .._plan_pricing_fixtures import BASE, KEY, SCOPE, entry, sample

ROWS = 3000
# Priced requests per capture. With one, a full read took 130-200 ms on an idle
# development machine, too close to 4 * LAG_FLOOR; with five it takes 0.7 s or more.
ENTRIES = 5
LAG_FLOOR = 0.05  # scheduling noise a loaded machine adds to a 5 ms sleep


def _fmt(moment) -> str:
    return moment.strftime("%Y-%m-%d %H:%M:%S.%f")


def _history(rows: int) -> tuple[list[tuple], list[tuple], list[tuple]]:
    """(quota, plan, price) rows: one capture every 30 s, ENTRIES priced requests each."""
    template = sample(start=BASE, end=BASE + timedelta(seconds=30),
                      entries=[entry(f"template-{k}", at=BASE + timedelta(seconds=15)) for k in range(ENTRIES)],
                      price_catalog=history_catalog()).model_dump(mode="json")
    quota, plan, price = [], [], []
    percent, tokens, previous = 10, 0, BASE
    for index in range(rows):
        at = BASE + timedelta(seconds=30 * (index + 1))
        percent = min(100, 10 + index // 60)
        tokens += 1000
        sid = f"s{index:06d}"
        common = {"snapshot_id": sid, "account_key": KEY, "limit_id": "codex", "window_duration_ms": WEEK,
                  "resets_at": (BASE + timedelta(days=5)).isoformat(), "observed_at": at.isoformat(),
                  "used_percent": percent}
        quota.append((sid, KEY, _fmt(at), json.dumps({**common, "used_percent": str(percent),
                                                      "source": "codex_app_server"})))
        plan.append((sid, KEY, _fmt(at), json.dumps({
            **common, "source": "codex_local_logs", "activity_observed_at": at.isoformat(),
            "activity_tokens": tokens, "activity_scope": SCOPE, "activity_binding_at": BASE.isoformat(),
            "activity_provider_configured_at": None,
        })))
        pricing = copy.deepcopy(template)
        renamed = {f"template-{k}": f"resp-{index:06d}-{k}" for k in range(ENTRIES)}
        pricing["interval_start"], pricing["interval_end"] = previous.isoformat(), at.isoformat()
        for line in pricing["entries"]:
            line["occurred_at"] = (at - timedelta(seconds=10)).isoformat()
            line["request"]["request_id"] = renamed[line["request"]["request_id"]]
        for item in pricing["quotes"][0]["items"]:
            item["request_id"] = renamed[item["request_id"]]
        price.append((sid, KEY, _fmt(at), json.dumps({
            **common, "source": "codex_local_logs", "activity_scope": SCOPE,
            "activity_binding_at": BASE.isoformat(), "activity_usd": None,
            "prediction_activity_usd": None, "pricing": pricing,
        })))
        previous = at
    return quota, plan, price


def _seed(database, rows: int = ROWS) -> None:
    """Rows as an upgraded database has them: no summaries yet."""
    quota, plan, price = _history(rows)
    con = sqlite3.connect(database)
    try:
        con.execute(
            "INSERT INTO account_usage_accounts (account_key, created_at, payload) VALUES (?, ?, ?)",
            (KEY, _fmt(BASE), json.dumps({"account_key": KEY, "label": "Load", "created_at": BASE.isoformat()})),
        )
        con.executemany("INSERT INTO account_usage_snapshots VALUES (?, ?, ?, ?)", quota)
        con.executemany("INSERT INTO account_plan_snapshots VALUES (?, ?, ?, ?)", plan)
        con.executemany("INSERT INTO account_plan_price_snapshots VALUES (?, ?, ?, ?)", price)
        con.commit()
    finally:
        con.close()


class _LagProbe:
    def __init__(self) -> None:
        self.worst = 0.0

    async def run(self, stop: asyncio.Event) -> None:
        loop = asyncio.get_running_loop()
        while not stop.is_set():
            before = loop.time()
            await asyncio.sleep(0.005)
            self.worst = max(self.worst, loop.time() - before - 0.005)


async def _worst_lag_during(call):
    """Run ``call()`` on this loop; return the longest the loop went unserved, and the result."""
    stop, probe = asyncio.Event(), _LagProbe()
    ticker = asyncio.create_task(probe.run(stop))
    await asyncio.sleep(0.02)
    try:
        result = await call()
    finally:
        stop.set()
        await ticker
    return probe.worst, result


async def _full_read_seconds(repository: AccountUsageRepository) -> float:
    """Calibration: what one full-history read, as the pre-fix code did it, costs here now."""
    started = time.monotonic()
    await repository.list_plan_price_snapshots(KEY)
    return time.monotonic() - started


async def test_the_account_page_does_not_block_the_loop_on_history(tmp_path):
    location = tmp_path / "page.sqlite"
    repository = AccountUsageRepository(f"sqlite+aiosqlite:///{location}")
    await repository.init_db()
    _seed(location)
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[routes.get_account_repository] = lambda: repository
    detail = f"/api/account-usage/{KEY}?include_pricing=false"
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            # Warm-up outside the measured window: FastAPI builds a route's state on
            # its first match, which would otherwise be measured here.
            assert (await client.get(f"/api/account-usage/{'f' * 64}?include_pricing=false")).status_code == 404
            full_read = await _full_read_seconds(repository)
            bound = max(0.25 * full_read, LAG_FLOOR)

            # First read after an upgrade: the summaries are rebuilt from the rows, off the loop.
            worst, response = await _worst_lag_during(lambda: client.get(detail))
            assert response.status_code == 200, response.text[:300]
            data = response.json()["data"]
            assert data["snapshot_count"] == ROWS and len(data["pricing_plan_estimates"]) == 1
            # Pre-fix, the page read the whole history on the loop: the lag was about
            # one full read. Off the loop it stays in the low milliseconds.
            assert worst < bound, f"rebuild read blocked the loop {worst * 1000:.0f}ms; " \
                f"one full read costs {full_read * 1000:.0f}ms"

            # Steady state: every refresh reads O(1) rows.
            worst, response = await _worst_lag_during(lambda: client.get(detail))
            assert response.status_code == 200
            assert response.json()["data"] == data
            assert worst < bound, f"steady read blocked the loop {worst * 1000:.0f}ms; " \
                f"one full read costs {full_read * 1000:.0f}ms"
    finally:
        await engine_pool.get_engine(repository._db_url).dispose()
    assert 0.25 * full_read > LAG_FLOOR, f"history too small to tell a full read apart ({full_read * 1000:.0f}ms)"


async def test_a_monitor_round_does_not_block_the_loop_on_history(tmp_path):
    location = tmp_path / "round.sqlite"
    repository = AccountUsageRepository(f"sqlite+aiosqlite:///{location}")
    await repository.init_db()
    _seed(location)
    try:
        await repository.summaries(KEY)  # upgrade rebuild happens once, off the loop
        full_read = await _full_read_seconds(repository)

        latest = await repository.latest_plan_snapshot(KEY)
        at = latest.observed_at + timedelta(seconds=30)
        plan = PlanUsageSnapshot.model_validate({
            **latest.model_dump(mode="python"), "snapshot_id": "new", "observed_at": at,
            "activity_observed_at": at, "activity_tokens": latest.activity_tokens + 10,
        })

        async def one_round():
            # One round's history-dependent work: the latest token sample, the price
            # window and its cycle step, then the commit that advances the summaries.
            assert (await repository.latest_plan_snapshot(KEY)).snapshot_id == latest.snapshot_id
            prices = await local_plan_capture._capture_prices(
                [plan], repository=repository, generation="g" * 64, catalog=history_catalog(), read_views={},
            )
            quota = (await repository.get_snapshot(latest.snapshot_id)).model_copy(update={
                "snapshot_id": "new", "observed_at": at, "used_percent": Decimal(latest.used_percent),
            })
            await repository.save_capture(
                await repository.get_account(KEY), [quota], plan_snapshots=[plan], pricing_plan_snapshots=prices,
            )
            return prices

        worst, prices = await _worst_lag_during(one_round)
        assert prices[0].prediction_activity_usd is not None
        assert worst < max(0.25 * full_read, LAG_FLOOR), (
            f"round blocked the loop {worst * 1000:.0f}ms; one full read costs {full_read * 1000:.0f}ms"
        )
        assert 0.25 * full_read > LAG_FLOOR, f"history too small to tell a full read apart ({full_read * 1000:.0f}ms)"
        stored = await repository.get_price_snapshot("new")
        assert stored == PricingPlanSnapshot.model_validate(prices[0].model_dump(mode="python"))
        assert order_key(stored) > order_key(await repository.get_price_snapshot(latest.snapshot_id))
    finally:
        await engine_pool.get_engine(repository._db_url).dispose()


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    monkeypatch.delenv("AITEAM_PRICING_CATALOG", raising=False)
