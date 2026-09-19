"""USD plan responses are computed from persisted pricing evidence."""

import sqlite3
from contextlib import asynccontextmanager
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aiteam.api.routes import account_usage as routes
from aiteam.storage import account_monitor as monitor_storage
from aiteam.storage.account_monitor import MonitorRepository
from aiteam.storage.account_usage import AccountUsageRepository
from aiteam.storage.connection import close_db

from .._plan_pricing_fixtures import BASE, KEY, account, entry, price_snapshot, quota


@pytest.fixture
def client(tmp_path, monkeypatch):
    state = SimpleNamespace(
        now=BASE, previous_at=BASE, previous_usd=Decimal(0), entries=[], percent=20,
        complete=True, include_price=True, expire_capture=False,
    )
    repository = AccountUsageRepository(f"sqlite+aiosqlite:///{tmp_path / 'prices.sqlite'}")
    monkeypatch.setattr(routes, "utc_now", lambda: state.now)
    monkeypatch.setattr(monitor_storage, "utc_now", lambda: state.now)

    async def capture(*, repository=None):
        value = price_snapshot(
            str(uuid4()), at=state.now, start=state.previous_at, previous_usd=state.previous_usd,
            entries=state.entries, percent=state.percent, complete=state.complete,
        )
        state.previous_at, state.previous_usd = state.now, value.activity_usd
        if state.expire_capture:
            state.now += timedelta(seconds=61)
        if state.include_price:
            return account(), [quota(value)], [], [value]
        return account(), [quota(value)], []

    def no_legacy_catalog():
        raise AssertionError("plan price reads must not reload the legacy batch catalog")

    monkeypatch.setattr(routes, "capture_account", capture)
    monkeypatch.setattr(routes, "_catalog", no_legacy_catalog)

    @asynccontextmanager
    async def lifespan(app):
        await repository.init_db()
        await MonitorRepository(repository._db_url).init_db()
        yield
        await close_db()

    app = FastAPI(lifespan=lifespan)
    app.include_router(routes.router)
    app.dependency_overrides[routes.get_account_repository] = lambda: repository
    with TestClient(app) as connected:
        yield connected, state, repository


def test_capture_mixed_models_then_cross_repository_get_preserves_exact_dollars(client):
    connected, state, repository = client
    initial = connected.post("/api/account-usage/capture", json={})
    assert initial.status_code == 200, initial.text
    assert initial.json()["data"]["pricing_plan_estimates"][0]["status"] == "collecting"
    state.now += timedelta(seconds=1)
    state.entries, state.percent = [entry(), entry("b", model="model-b")], 21
    response = connected.post("/api/account-usage/capture", json={})
    assert response.status_code == 200, response.text
    result = response.json()["data"]["pricing_plan_estimates"][0]
    assert result["status"] == "estimated" and Decimal(result["estimated_total_usd"]) == Decimal("1.464")
    assert Decimal(result["delta_usd"]) == Decimal(".01464")
    assert result["used_percent"] == 21 and result["delta_used_percent"] == 1
    assert result["pricing_mode"] == "standard_equivalent"
    assert result["catalog_version"] == "fixture-v1" and len(result["catalog_sha256"]) == 64
    connected.app.dependency_overrides[routes.get_account_repository] = lambda: AccountUsageRepository(
        repository._db_url,
    )
    later = connected.get(f"/api/account-usage/{KEY}", params={"include_pricing": "false"})
    assert later.status_code == 200, later.text
    assert later.json()["data"]["pricing_plan_estimates"] == [result]
    assert later.json()["data"]["estimates"] == []


def test_incomplete_prices_predict_known_contributions_with_current_percentage(client):
    connected, state, _ = client
    assert connected.post("/api/account-usage/capture", json={}).status_code == 200
    state.now += timedelta(seconds=1)
    state.entries, state.percent, state.complete = [entry(), entry("missing", model="unknown")], 21, False
    response = connected.post("/api/account-usage/capture", json={})
    assert response.status_code == 200, response.text
    result = response.json()["data"]["pricing_plan_estimates"][0]
    assert result["status"] == "estimated" and result["reason_code"] == "pricing_incomplete"
    assert result["used_percent"] == 21
    assert Decimal(result["estimated_total_usd"]) == Decimal(".294")
    assert Decimal(result["delta_usd"]) == Decimal(".00294")
    assert result["prediction_basis"] == "cycle_anchor_missing_zero"


def test_legacy_three_item_capture_does_not_backfill_dollars(client):
    connected, state, _ = client
    state.include_price = False
    response = connected.post("/api/account-usage/capture", json={})
    assert response.status_code == 200, response.text
    assert response.json()["data"]["pricing_plan_estimates"] == []
    later = connected.get(f"/api/account-usage/{KEY}", params={"include_pricing": "false"})
    assert later.status_code == 200
    assert later.json()["data"]["pricing_plan_estimates"] == []


def test_expired_manual_capture_does_not_save_price_or_create_an_account(client):
    connected, state, repository = client
    state.expire_capture = True
    response = connected.post("/api/account-usage/capture", json={})
    assert response.status_code == 409
    assert connected.portal.call(repository.list_plan_price_snapshots, KEY) == []
    assert connected.portal.call(repository.list_accounts) == []


@pytest.mark.parametrize("latest_percent", [22, 20])
def test_incomplete_capture_recalculates_cycle_without_writing_old_payloads(
    client, tmp_path, latest_percent,
):
    connected, state, repository = client
    assert connected.post("/api/account-usage/capture", json={}).status_code == 200
    state.now += timedelta(seconds=1)
    state.entries, state.percent = [entry(), entry("b", model="model-b")], 21
    good = connected.post("/api/account-usage/capture", json={}).json()["data"]["pricing_plan_estimates"][0]
    state.now += timedelta(seconds=1)
    state.entries = [entry("unknown", at=state.now, model="unknown")]
    state.percent, state.complete = latest_percent, False
    response = connected.post("/api/account-usage/capture", json={})
    assert response.status_code == 200, response.text
    current = response.json()["data"]["pricing_plan_estimates"][0]
    assert current["used_percent"] == latest_percent
    if latest_percent > 21:
        assert Decimal(current["estimated_total_usd"]) == Decimal(good["estimated_total_usd"]) / 2
        assert current["last_estimated_total_usd"] == current["estimated_total_usd"]
        assert current["last_estimate_observed_at"] == current["observed_at"]
        assert current["start_snapshot_id"] == good["start_snapshot_id"]
    else:
        assert current["estimated_total_usd"] is None and current["delta_usd"] == "0"
        assert current["last_estimated_total_usd"] is None and current["last_estimate_observed_at"] is None

    def stored_payloads():
        with sqlite3.connect(f"file:{tmp_path / 'prices.sqlite'}?mode=ro", uri=True) as database:
            return database.execute("SELECT id, payload FROM account_plan_price_snapshots ORDER BY id").fetchall()

    before = stored_payloads()
    assert all("last_estimated_total_usd" not in payload for _, payload in before)
    connected.app.dependency_overrides[routes.get_account_repository] = lambda: AccountUsageRepository(
        repository._db_url,
    )
    result = connected.get(f"/api/account-usage/{KEY}", params={"include_pricing": "false"})
    assert result.status_code == 200, result.text
    assert result.json()["data"]["pricing_plan_estimates"] == [current]
    assert stored_payloads() == before
