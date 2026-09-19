"""Plan capacity is computed from persisted native observations, without pricing."""

import sqlite3
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from aiteam.api.routes import account_usage as routes
from aiteam.api.routes import pricing as pricing_routes
from aiteam.storage import account_monitor as monitor_storage
from aiteam.storage.account_monitor import MonitorRepository
from aiteam.storage.account_usage import AccountUsageRepository
from aiteam.storage.connection import close_db
from aiteam.storage.models import AccountPlanSnapshotModel, AccountUsageBatchModel
from aiteam.types import PlanUsageSnapshot, PricingAccount, PricingQuotaSnapshot

KEY = "a" * 64


@pytest.fixture
def client(tmp_path, monkeypatch):
    state = SimpleNamespace(
        now=datetime(2026, 9, 14, 8, tzinfo=UTC), tokens=1_000_000, percent=20,
        source="codex_account_activity", binding_at=None,
    )
    repository = AccountUsageRepository(f"sqlite+aiosqlite:///{tmp_path / 'plans.sqlite'}")
    monkeypatch.setattr(routes, "utc_now", lambda: state.now)
    monkeypatch.setattr(monitor_storage, "utc_now", lambda: state.now)

    async def capture(*, repository=None):
        account = PricingAccount(account_key=KEY, label="Native account", created_at=state.now)
        windows = []
        plans = []
        for minutes in (300, 10080):
            data = dict(
                snapshot_id=str(uuid4()), account_key=KEY, limit_id="codex",
                window_duration_ms=minutes * 60_000, observed_at=state.now,
                resets_at=datetime(2026, 9, 14, 12, tzinfo=UTC) if minutes == 300
                else datetime(2026, 9, 18, 12, tzinfo=UTC),
                used_percent=state.percent,
            )
            windows.append(PricingQuotaSnapshot(**data, source="codex_app_server"))
            plans.append(PlanUsageSnapshot(
                **data, activity_tokens=state.tokens, activity_scope="f" * 64,
                activity_observed_at=state.now, source=state.source,
                activity_binding_at=state.binding_at,
            ))
        return account, windows, plans

    monkeypatch.setattr(routes, "capture_account", capture)

    def no_price():
        raise AssertionError("plan capacity must not require a price catalog")

    monkeypatch.setattr(routes, "_catalog", no_price)

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
        yield connected, state


def test_capture_then_get_preserves_percentage_without_inventing_capacity(client):
    connected, state = client
    first = connected.post("/api/account-usage/capture", json={})
    assert first.status_code == 200, first.text
    initial = first.json()["data"]["plan_estimates"]
    assert len(initial) == 2
    assert all(item["status"] == "unavailable" for item in initial)
    assert all(item["reason_code"] == "activity_coverage_unknown" for item in initial)
    state.now += timedelta(minutes=10)
    state.tokens += 1_000_000
    state.percent += 5
    second = connected.post("/api/account-usage/capture", json={})
    assert second.status_code == 200, second.text
    expected = second.json()["data"]["plan_estimates"]
    later = connected.get(f"/api/account-usage/{KEY}").json()["data"]
    assert later["plan_estimates"] == expected
    assert len(later["snapshots"]) == 4
    assert later["estimates"] == []
    assert {item["window_duration_ms"] for item in expected} == {18_000_000, 604_800_000}
    for item in expected:
        assert item["estimated_total_tokens"] is None
        assert item["used_percent"] == 25
        assert item["delta_tokens"] is None
        assert item["delta_used_percent"] is None
        assert item["status"] == "unavailable"
        assert item["reason_code"] == "activity_coverage_unknown"


def test_old_snapshot_json_remains_unchanged_after_cross_repository_api_read(client, tmp_path):
    connected, state = client
    assert connected.post("/api/account-usage/capture", json={}).status_code == 200
    state.now += timedelta(minutes=10)
    state.tokens += 1_000_000
    state.percent = 54
    assert connected.post("/api/account-usage/capture", json={}).status_code == 200
    repository = connected.app.dependency_overrides[routes.get_account_repository]()
    legacy_fields = {
        "snapshot_id", "account_key", "limit_id", "window_duration_ms", "resets_at",
        "observed_at", "used_percent", "activity_observed_at", "activity_tokens",
        "activity_scope", "source",
    }

    async def prepare_legacy_fixture():
        async with repository._write_session() as session:
            stored = (await session.scalars(select(AccountPlanSnapshotModel))).all()
            for row in stored:
                row.payload = {key: value for key, value in row.payload.items() if key in legacy_fields}

    def stored_payloads():
        with sqlite3.connect(f"file:{tmp_path / 'plans.sqlite'}?mode=ro", uri=True) as database:
            return database.execute("SELECT id, payload FROM account_plan_snapshots ORDER BY id").fetchall()

    connected.portal.call(prepare_legacy_fixture)
    before = stored_payloads()
    assert len(before) == 4
    connected.app.dependency_overrides[routes.get_account_repository] = lambda: AccountUsageRepository(
        repository._db_url,
    )
    response = connected.get(f"/api/account-usage/{KEY}", params={"include_pricing": "false"})
    assert response.status_code == 200, response.text
    for item in response.json()["data"]["plan_estimates"]:
        assert item["status"] == "unavailable"
        assert item["reason_code"] == "activity_coverage_unknown"
        assert item["used_percent"] == 54
        assert item["estimated_total_tokens"] is None
        assert item["delta_tokens"] is None
    assert stored_payloads() == before


def test_capacity_is_not_returned_for_another_account(client):
    connected, _ = client
    assert connected.post("/api/account-usage/capture", json={}).status_code == 200
    assert connected.get(f"/api/account-usage/{'b' * 64}").status_code == 404


@pytest.mark.parametrize(("seconds", "tokens", "percent", "capacity"), [
    (600, 1_000_000, 25, 20_000_000),
    (1, 1000, 21, 100_000),
    (60, 1_628_783, 21, 162_878_300),
])
def test_local_zero_binding_then_delta_produces_persisted_plan_capacity(client, seconds, tokens, percent, capacity):
    connected, state = client
    state.source, state.binding_at, state.tokens = "codex_local_logs", state.now, 0
    first = connected.post("/api/account-usage/capture", json={})
    assert first.status_code == 200, first.text
    assert all(item["status"] == "collecting" for item in first.json()["data"]["plan_estimates"])
    state.now += timedelta(seconds=seconds)
    state.tokens, state.percent = tokens, percent
    second = connected.post("/api/account-usage/capture", json={})
    assert second.status_code == 200, second.text
    expected = second.json()["data"]["plan_estimates"]
    assert len(expected) == 2
    for item in expected:
        assert item["status"] == "estimated" and item["source"] == "codex_local_logs"
        assert item["estimated_total_tokens"] == capacity
        assert item["used_percent"] == percent and item["delta_tokens"] == tokens
        assert item["delta_used_percent"] == percent - 20
        assert item["reason_code"] is None
    repository = connected.app.dependency_overrides[routes.get_account_repository]()
    connected.app.dependency_overrides[routes.get_account_repository] = lambda: AccountUsageRepository(
        repository._db_url,
    )
    later = connected.get(f"/api/account-usage/{KEY}", params={"include_pricing": "false"})
    assert later.status_code == 200, later.text
    assert later.json()["data"]["plan_estimates"] == expected


def test_local_binding_does_not_use_existing_raw_summary_as_baseline(client):
    connected, state = client
    assert connected.post("/api/account-usage/capture", json={}).status_code == 200
    state.now += timedelta(minutes=10)
    state.source, state.binding_at, state.tokens, state.percent = "codex_local_logs", state.now, 0, 25
    response = connected.post("/api/account-usage/capture", json={})
    assert response.status_code == 200, response.text
    for item in response.json()["data"]["plan_estimates"]:
        assert item["status"] == "collecting" and item["source"] == "codex_local_logs"
        assert item["estimated_total_tokens"] is None and item["delta_tokens"] is None


def test_expired_window_never_reuses_old_percentage_as_current(client):
    connected, state = client
    assert connected.post("/api/account-usage/capture", json={}).status_code == 200
    state.now += timedelta(hours=5)
    plans = connected.get(f"/api/account-usage/{KEY}").json()["data"]["plan_estimates"]
    short = next(item for item in plans if item["window_duration_ms"] == 18_000_000)
    assert short["status"] == "expired"
    assert short["used_percent"] is None
    assert short["estimated_total_tokens"] is None


@pytest.mark.parametrize("failure", ["catalog", "missing_snapshot", "invalid_batch"])
def test_plan_reads_survive_persisted_legacy_pricing_failures(client, monkeypatch, tmp_path, failure):
    connected, state = client
    monkeypatch.delenv("AITEAM_PRICING_CATALOG", raising=False)
    monkeypatch.setattr(routes, "_catalog", pricing_routes._catalog)
    first = connected.post("/api/account-usage/capture", json={}).json()["data"]
    occurred_at = state.now + timedelta(minutes=5)
    state.now += timedelta(minutes=10)
    state.tokens += 1_000_000
    state.percent += 5
    second = connected.post("/api/account-usage/capture", json={}).json()["data"]
    start = next(item for item in first["snapshots"] if item["window_duration_ms"] == 604_800_000)
    end = next(item for item in second["snapshots"] if item["window_duration_ms"] == 604_800_000)
    saved = connected.post(f"/api/account-usage/{KEY}/batches", json={
        "batch_id": "legacy-batch", "account_key": KEY,
        "start_snapshot_id": start["snapshot_id"], "end_snapshot_id": end["snapshot_id"],
        "coverage": "local_only", "coverage_statement": "Isolated legacy sample",
        "coverage_confirmed_at": None,
        "entries": [{
            "occurred_at": occurred_at.isoformat(),
            "request": {
                "request_id": "legacy-request", "model": "gpt-6-astra", "service_tier": "standard",
                "input_tokens": 1000, "output_tokens": 100,
                "cached_input_tokens": 0, "cache_write_input_tokens": 0,
            },
        }],
    })
    assert saved.status_code == 200, saved.text
    url = f"/api/account-usage/{KEY}"
    assert connected.get(url).json()["data"]["estimates"] == [saved.json()["data"]]

    if failure == "catalog":
        monkeypatch.setenv("AITEAM_PRICING_CATALOG", str(tmp_path / "missing-catalog.json"))
    else:
        repository = connected.app.dependency_overrides[routes.get_account_repository]()

        async def damage_legacy_batch():
            async with repository._write_session() as session:
                row = await session.get(AccountUsageBatchModel, "legacy-batch")
                changes = {"start_snapshot_id": "missing"} if failure == "missing_snapshot" else {
                    "account_key": "invalid-account-key",
                }
                row.payload = {**row.payload, **changes}

        connected.portal.call(damage_legacy_batch)

    response = connected.get(url, params={"include_pricing": "false"})
    assert response.status_code == 200, response.text
    assert response.json()["data"]["estimates"] == []
    assert response.json()["data"]["plan_estimates"] == second["plan_estimates"]
    assert all(item["estimated_total_tokens"] is None for item in second["plan_estimates"])
    assert all(item["reason_code"] == "activity_coverage_unknown" for item in second["plan_estimates"])
    for suffix in ("", "?include_pricing=true"):
        if failure == "invalid_batch":
            with pytest.raises(ValueError):
                connected.get(url + suffix)
        else:
            assert connected.get(url + suffix).status_code == (503 if failure == "catalog" else 409)
