"""User-facing monitor configuration, isolated from native login and shared OS."""

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aiteam.api.routes import account_usage as routes
from aiteam.clock import utc_now
from aiteam.storage.account_monitor import MonitorRepository
from aiteam.storage.account_usage import AccountUsageRepository
from aiteam.storage.connection import close_db
from aiteam.storage.models import AccountUsageMonitorModel
from aiteam.types import PricingAccount, PricingQuotaSnapshot

KEY = "a" * 64
OTHER = "b" * 64


class ConfigurationObserver:
    """Does not pretend to execute a capture; only observes route notification."""

    is_running = True

    def __init__(self):
        self.changes = []

    async def settings_changed(self, account_key):
        self.changes.append(account_key)


@pytest.fixture
def client(tmp_path):
    url = f"sqlite+aiosqlite:///{tmp_path / 'account-monitor.sqlite'}"
    accounts = AccountUsageRepository(url)
    monitors = MonitorRepository(url)
    observer = ConfigurationObserver()

    @asynccontextmanager
    async def lifespan(app):
        await accounts.init_db()
        await monitors.init_db()
        for key in (KEY, OTHER):
            await accounts.upsert_account(PricingAccount(
                account_key=key, label="测试绑定", created_at=datetime(2026, 9, 14, tzinfo=UTC),
            ))
        app.state.account_monitor_runner = observer
        yield
        await close_db()

    app = FastAPI(lifespan=lifespan)
    app.include_router(routes.router)
    app.dependency_overrides[routes.get_account_repository] = lambda: accounts
    with TestClient(app) as connection:
        connection.observer = observer
        yield connection


def test_configuration_defaults_to_disabled_without_a_claimed_capture(client):
    response = client.get(f"/api/account-usage/{KEY}/monitor")
    assert response.status_code == 200
    state = response.json()["data"]
    assert state["settings"] == {"enabled": False, "interval_ms": 1800000}
    assert state["status"] == "disabled"
    assert state["runtime_running"] is True
    assert state["last_finished_at"] is None
    assert client.observer.changes == []


@pytest.mark.parametrize("previous", [None, False, True])
def test_manual_capture_defaults_on_and_preserves_explicit_settings(client, monkeypatch, previous):
    url = f"/api/account-usage/{KEY}/monitor"
    if previous is not None:
        assert client.put(url, json={"enabled": previous, "interval_ms": 300000}).status_code == 200
    before = client.get(url).json()["data"]

    async def capture(*, repository):
        now = utc_now()
        return PricingAccount(account_key=KEY, label="Native source", created_at=now), [
            PricingQuotaSnapshot(
                snapshot_id=str(uuid4()), account_key=KEY, limit_id="codex", used_percent="20",
                window_duration_ms=604800000, resets_at=now + timedelta(days=2),
                observed_at=now, source="codex_app_server",
            ),
        ]

    monkeypatch.setattr(routes, "capture_account", capture)
    response = client.post("/api/account-usage/capture")
    assert response.status_code == 200, response.text
    state = client.get(url).json()["data"]
    if previous is None:
        assert state["settings"] == {"enabled": True, "interval_ms": 1800000}
        assert state["revision"] == 1 and state["status"] == "waiting"
        assert state["next_run_at"] is not None and state["last_finished_at"] is not None
    else:
        assert state == before
    assert not client.get(f"/api/account-usage/{OTHER}/monitor").json()["data"]["settings"]["enabled"]


def test_enable_interval_update_and_disable_survive_separate_requests(client):
    url = f"/api/account-usage/{KEY}/monitor"
    enabled = client.put(url, json={"enabled": True, "interval_ms": 300000})
    assert enabled.status_code == 200, enabled.text
    state = client.get(url).json()["data"]
    assert state["settings"] == {"enabled": True, "interval_ms": 300000}
    assert state["status"] == "waiting"
    assert state["last_finished_at"] is None  # Configuration is not a successful schedule run.
    first_revision = state["revision"]
    response = client.put(url, json={"enabled": True, "interval_ms": 600000})
    assert response.status_code == 200
    assert response.json()["data"]["revision"] > first_revision
    response = client.put(url, json={"enabled": False, "interval_ms": 600000})
    assert response.status_code == 200
    after = client.get(url).json()["data"]
    assert after["status"] == "disabled" and not after["settings"]["enabled"]
    assert client.observer.changes == [KEY, KEY, KEY]


def test_source_is_not_silently_reassigned_to_another_account(client):
    assert client.put(f"/api/account-usage/{KEY}/monitor", json={"enabled": True}).status_code == 200
    response = client.put(f"/api/account-usage/{OTHER}/monitor", json={"enabled": True})
    assert response.status_code == 409
    assert client.get(f"/api/account-usage/{KEY}/monitor").json()["data"]["settings"]["enabled"]
    assert not client.get(f"/api/account-usage/{OTHER}/monitor").json()["data"]["settings"]["enabled"]


@pytest.mark.parametrize("settings", [
    {"enabled": True, "interval_ms": 29999},
    {"enabled": True, "interval_ms": 1800001},
    {"enabled": True, "interval_ms": 86400001},
    {"enabled": True, "interval_ms": True},
    {"enabled": True, "interval_ms": 300000.5},
    {"enabled": True, "password": "must-not-be-accepted"},
])
def test_bad_or_secret_configuration_is_rejected(client, settings):
    response = client.put(f"/api/account-usage/{KEY}/monitor", json=settings)
    assert response.status_code == 422
    assert not client.get(f"/api/account-usage/{KEY}/monitor").json()["data"]["settings"]["enabled"]
    assert client.observer.changes == []


@pytest.mark.parametrize("interval_ms", [30000, 1800000])
def test_new_period_boundaries_survive_api_roundtrip(client, interval_ms):
    url = f"/api/account-usage/{KEY}/monitor"
    response = client.put(url, json={"enabled": True, "interval_ms": interval_ms})
    assert response.status_code == 200, response.text
    assert client.get(url).json()["data"]["settings"]["interval_ms"] == interval_ms


def test_openapi_advertises_new_period_range_and_existing_default(client):
    schema = client.get("/openapi.json").json()["components"]["schemas"]["PricingMonitorSettings"]
    period = schema["properties"]["interval_ms"]
    assert period["minimum"] == 30000
    assert period["maximum"] == 1800000
    assert period["default"] == 1800000


def test_legacy_day_period_can_be_read_and_paused_without_implicit_migration(client):
    url = f"/api/account-usage/{KEY}/monitor"
    assert client.put(url, json={"enabled": True, "interval_ms": 300000}).status_code == 200
    accounts = client.app.dependency_overrides[routes.get_account_repository]()

    async def install_old_payload():
        async with accounts._write_session() as session:
            row = await session.get(AccountUsageMonitorModel, KEY)
            row.payload = {**row.payload, "settings": {"enabled": True, "interval_ms": 86400000}}

    client.portal.call(install_old_payload)
    before = client.get(url)
    assert before.status_code == 200, before.text
    assert before.json()["data"]["settings"]["interval_ms"] == 86400000
    response = client.put(url, json={"enabled": False})
    assert response.status_code == 200, response.text
    paused = client.get(url).json()["data"]
    assert paused["settings"] == {"enabled": False, "interval_ms": 86400000}
    assert paused["status"] == "disabled" and paused["next_run_at"] is None
    assert client.put(url, json={"enabled": True}).status_code == 409
    assert client.put(url, json={"enabled": False, "interval_ms": 86400000}).status_code == 422
    assert client.get(url).json()["data"]["revision"] == paused["revision"]
    changed = client.put(url, json={"enabled": True, "interval_ms": 30000})
    assert changed.status_code == 200, changed.text
    assert client.get(url).json()["data"]["settings"]["interval_ms"] == 30000


def test_pause_without_period_preserves_five_minutes(client):
    url = f"/api/account-usage/{KEY}/monitor"
    assert client.put(url, json={"enabled": True, "interval_ms": 300000}).status_code == 200
    paused = client.put(url, json={"enabled": False})
    assert paused.status_code == 200
    assert paused.json()["data"]["settings"] == {"enabled": False, "interval_ms": 300000}


def test_duplicate_config_keys_do_not_hide_the_users_intent(client):
    response = client.put(
        f"/api/account-usage/{KEY}/monitor", content='{"enabled":false,"enabled":true}',
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 422


def test_runtime_absence_is_distinct_from_a_saved_setting(client):
    client.app.state.account_monitor_runner = None
    url = f"/api/account-usage/{KEY}/monitor"
    state = client.get(url).json()["data"]
    assert state["runtime_running"] is False
    assert client.put(url, json={"enabled": True}).status_code == 503
    assert not client.get(url).json()["data"]["settings"]["enabled"]
    assert client.put(url, json={"enabled": False}).status_code == 200


def test_unknown_account_requires_binding_first(client):
    url = f"/api/account-usage/{'f' * 64}/monitor"
    assert client.get(url).status_code == 404
    assert client.put(url, json={"enabled": True}).status_code == 404
