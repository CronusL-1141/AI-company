"""Account usage round trips use only an isolated SQLite database and fake capture."""

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aiteam.api.routes import account_usage as routes
from aiteam.services.codex_account_capture import CodexAccountCaptureError
from aiteam.storage.account_monitor import MonitorRepository
from aiteam.storage.account_usage import AccountUsageRepository
from aiteam.storage.connection import close_db
from aiteam.types import PricingAccount, PricingQuotaSnapshot

KEY = "a" * 64
OTHER = "b" * 64


def snapshot(identifier, percent, hour, key=KEY, **updates):
    values = dict(
        snapshot_id=identifier, account_key=key, limit_id="codex",
        used_percent=Decimal(percent), window_duration_ms=604800000,
        resets_at=datetime(2026, 9, 19, 8, tzinfo=UTC),
        observed_at=datetime(2026, 9, 14, hour, tzinfo=UTC), source="codex_app_server",
    )
    values.update(updates)
    return PricingQuotaSnapshot(**values)


def batch(identifier="batch-one", **updates):
    values = dict(
        batch_id=identifier, account_key=KEY,
        start_snapshot_id="start", end_snapshot_id="end",
        coverage="local_only", coverage_statement="本机脱敏样本，不代表账号全量",
        coverage_confirmed_at=None,
        entries=[{
            "occurred_at": "2026-09-14T05:30:00Z",
            "request": {
                "request_id": "response-one", "model": "gpt-6-astra",
                "service_tier": "standard", "input_tokens": 1000, "output_tokens": 100,
                "cached_input_tokens": 600, "cache_write_input_tokens": 0,
            },
        }],
    )
    values.update(updates)
    return values


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.delenv("AITEAM_PRICING_CATALOG", raising=False)
    repository = AccountUsageRepository(f"sqlite+aiosqlite:///{tmp_path / 'accounts.sqlite'}")

    @asynccontextmanager
    async def lifespan(app):
        await repository.init_db()
        await MonitorRepository(repository._db_url).init_db()
        for key in (KEY, OTHER):
            account = PricingAccount(
                account_key=key, label="测试账号" if key == KEY else "另一账号",
                created_at=datetime(2026, 9, 14, 5, tzinfo=UTC),
            )
            await repository.save_capture(account, [snapshot(f"{key}-first", "12", 5, key)])
        await repository.add_snapshot(snapshot("start", "12", 5))
        await repository.add_snapshot(snapshot("end", "16", 6))
        yield
        await close_db()

    app = FastAPI(lifespan=lifespan)
    app.include_router(routes.router)
    app.dependency_overrides[routes.get_account_repository] = lambda: repository
    with TestClient(app) as connected:
        yield connected


def test_accounts_and_native_snapshots_remain_separate(client):
    accounts = client.get("/api/account-usage").json()["data"]["accounts"]
    assert {a["account_key"] for a in accounts} == {KEY, OTHER}
    data = client.get(f"/api/account-usage/{KEY}").json()["data"]
    assert all(s["account_key"] == KEY for s in data["snapshots"])
    assert data["estimates"] == []
    assert "email" not in str(data)


def test_local_sample_is_persistent_but_never_a_whole_account_estimate(client):
    response = client.post(f"/api/account-usage/{KEY}/batches", json=batch())
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["status"] == "sample_only"
    assert data["estimated_full_week_usd"] is None
    assert data["quote"]["complete"]
    assert Decimal(data["quote"]["total_usd"]) > 0
    later = client.get(f"/api/account-usage/{KEY}").json()["data"]["estimates"]
    assert later == [data]


def test_user_confirmed_full_interval_is_conditionally_projected(client):
    payload = batch(
        coverage="account_complete",
        coverage_statement="我确认这段时间该账号所有客户端用量均包含在本批且均属于该账号",
        coverage_confirmed_at="2026-09-14T06:01:00Z",
    )
    response = client.post(f"/api/account-usage/{KEY}/batches", json=payload)
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["status"] == "conditional"
    assert Decimal(data["estimated_full_week_usd"]) == Decimal(data["quote"]["total_usd"]) * 25
    assert data["coverage_statement"] == payload["coverage_statement"]
    assert data["coverage_confirmed_at"]


def test_same_batch_retry_is_idempotent_but_cross_batch_request_duplicates_fail(client):
    url = f"/api/account-usage/{KEY}/batches"
    first = client.post(url, json=batch())
    assert first.status_code == 200, first.text
    assert client.post(url, json=batch()).json() == first.json()
    conflict = client.post(url, json=batch("different-batch"))
    assert conflict.status_code == 409
    assert len(client.get(f"/api/account-usage/{KEY}").json()["data"]["estimates"]) == 1


def test_immutable_batch_cannot_change_existing_coverage(client):
    url = f"/api/account-usage/{KEY}/batches"
    assert client.post(url, json=batch()).status_code == 200
    payload = batch(
        coverage="account_complete", coverage_statement="全部客户端同期已完整覆盖",
        coverage_confirmed_at="2026-09-14T06:01:00Z",
    )
    assert client.post(url, json=payload).status_code == 409


@pytest.mark.parametrize("changes", [
    {"account_key": OTHER}, {"start_snapshot_id": "missing"},
    {"start_snapshot_id": f"{OTHER}-first"}, {"end_snapshot_id": "start"},
    {"coverage": "account_complete"},
])
def test_bad_account_interval_or_confirmation_never_persists(client, changes):
    response = client.post(f"/api/account-usage/{KEY}/batches", json=batch(**changes))
    assert response.status_code in (409, 422), response.text
    assert client.get(f"/api/account-usage/{KEY}").json()["data"]["estimates"] == []


def test_request_time_outside_snapshot_interval_rejected(client):
    payload = batch()
    payload["entries"][0]["occurred_at"] = "2026-09-14T04:59:00Z"
    assert client.post(f"/api/account-usage/{KEY}/batches", json=payload).status_code == 409


def test_duplicate_json_keys_do_not_silently_drop_a_batch(client):
    response = client.post(
        f"/api/account-usage/{KEY}/batches",
        content='{"entries":[],"entries":[]}', headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 422


def test_missing_price_keeps_account_projection_unknown(client):
    payload = batch(
        coverage="account_complete", coverage_statement="全部客户端同期已完整覆盖",
        coverage_confirmed_at="2026-09-14T06:01:00Z",
    )
    payload["entries"][0]["request"]["model"] = "future-model-no-published-price"
    response = client.post(f"/api/account-usage/{KEY}/batches", json=payload)
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["estimated_full_week_usd"] is None
    assert data["quote"]["total_usd"] is None
    assert data["quote"]["missing_models"] == ["future-model-no-published-price"]


def test_capture_is_explicit_and_preserves_alias(client, monkeypatch):
    account = PricingAccount(
        account_key=KEY, label="Codex 自动标签", created_at=datetime(2026, 9, 14, 7, tzinfo=UTC),
    )
    called = []

    async def fake_capture():
        called.append(True)
        return account, [snapshot("native-new", "17", 7)]

    monkeypatch.setattr(routes, "capture_account", fake_capture)
    assert client.patch(f"/api/account-usage/{KEY}/label", json={"label": "我的主账号"}).status_code == 200
    client.get(f"/api/account-usage/{KEY}")
    assert called == []
    response = client.post("/api/account-usage/capture", json={})
    assert response.status_code == 200, response.text
    assert called == [True]
    assert response.json()["data"]["account"]["label"] == "我的主账号"
    data = client.get(f"/api/account-usage/{KEY}").json()["data"]
    assert any(s["snapshot_id"] == "native-new" for s in data["snapshots"])


def test_capture_failure_is_not_successful_zero_and_does_not_write(client, monkeypatch):
    async def failed_capture():
        raise CodexAccountCaptureError("原生账号采样不可用")

    monkeypatch.setattr(routes, "capture_account", failed_capture)
    before = client.get(f"/api/account-usage/{KEY}").json()
    response = client.post("/api/account-usage/capture", json={})
    assert response.status_code == 503
    assert response.json()["detail"] == "原生账号采样不可用"
    assert client.get(f"/api/account-usage/{KEY}").json() == before


def test_missing_account_and_blank_alias_fail_without_creating_accounts(client):
    assert client.get(f"/api/account-usage/{'c' * 64}").status_code == 404
    assert client.get("/api/account-usage/not-a-key").status_code == 422
    assert client.patch(f"/api/account-usage/{KEY}/label", json={"label": "  "}).status_code == 422
