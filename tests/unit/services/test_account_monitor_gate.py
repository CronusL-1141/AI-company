"""Due rounds without local Codex activity are skipped, within a 2-hour bound.

Founder ruling 2026-09-29 (task 551aee38): the monitor reads Codex, not this
session; with no new input or output it should not read again. Quota is shared
across the account's devices and resets on its own schedule, so an idle account
is still sampled every IDLE_FALLBACK (2 hours, ruled the same day: use on other
devices may show up that late), and right after a known window reset.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import FastAPI

from aiteam.api import account_monitor_lifecycle
from aiteam.services import account_monitor as monitor_module
from aiteam.services import codex_activity
from aiteam.services.account_monitor import IDLE_FALLBACK, AccountMonitorRunner
from aiteam.services.codex_usage_recorder import CodexUsageRecorder
from aiteam.storage import account_monitor as repository_module
from aiteam.storage.account_monitor import MonitorRepository
from aiteam.storage.account_usage import AccountUsageRepository
from aiteam.storage.engine_pool import engine_pool
from aiteam.types import PricingAccount, PricingMonitorSettings, PricingQuotaSnapshot

ACCOUNT = "a" * 64
INTERVAL = timedelta(seconds=30)


@pytest_asyncio.fixture
async def stores(tmp_path, monkeypatch):
    clock = SimpleNamespace(now=datetime(2026, 9, 29, 12, 0, tzinfo=UTC))
    monkeypatch.setattr(repository_module, "utc_now", lambda: clock.now)
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'gate.db'}"
    usage = AccountUsageRepository(database_url)
    monitor = MonitorRepository(database_url)
    await usage.init_db()
    await monitor.init_db()
    await usage.upsert_account(PricingAccount(account_key=ACCOUNT, label="User alias", created_at=clock.now))
    await monitor.configure(ACCOUNT, PricingMonitorSettings(enabled=True, interval_ms=30000))
    yield monitor, usage, clock
    await engine_pool.get_engine(database_url).dispose()


def _quota(observed_at, resets_at):
    return PricingQuotaSnapshot(
        snapshot_id=str(uuid4()), account_key=ACCOUNT, limit_id="codex", used_percent=Decimal(4),
        window_duration_ms=604800000, resets_at=resets_at, observed_at=observed_at, source="codex_app_server",
    )


async def _captured(usage, clock, *, ago, resets_in=timedelta(days=3)):
    at = clock.now - ago
    account = PricingAccount(account_key=ACCOUNT, label="User alias", created_at=at)
    await usage.save_capture(account, [_quota(at, at + resets_in)])
    return at


class _Capture:
    def __init__(self, clock):
        self.clock, self.calls = clock, 0

    async def __call__(self):
        self.calls += 1
        account = PricingAccount(account_key=ACCOUNT, label="Native", created_at=self.clock.now)
        return account, [_quota(self.clock.now, self.clock.now + timedelta(days=3))]


class _Probe:
    def __init__(self, answer):
        self.answer, self.since = answer, []

    async def __call__(self, since):
        self.since.append(since)
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


def _runner(monitor, clock, probe):
    capture = _Capture(clock)
    return AccountMonitorRunner(monitor, capture=capture, clock=lambda: clock.now, activity_probe=probe), capture


@pytest.mark.asyncio
async def test_idle_round_is_skipped_and_leaves_the_state_as_it_was(stores):
    monitor, usage, clock = stores
    last = await _captured(usage, clock, ago=timedelta(minutes=5))
    before = await monitor.get(ACCOUNT)
    probe = _Probe(False)
    runner, capture = _runner(monitor, clock, probe)

    assert await runner.tick() is True
    assert capture.calls == 0
    assert probe.since == [last]
    state = await monitor.get(ACCOUNT)
    assert state.last_skipped_at == clock.now
    assert state.status == before.status == "waiting"
    assert state.last_started_at == before.last_started_at
    assert state.last_finished_at == before.last_finished_at  # the page does not refresh estimates
    assert state.next_run_at == clock.now + INTERVAL
    assert await monitor.claim_due("someone-else", clock.now) is None  # not due, and no stale lease
    assert len(await usage.list_snapshots(ACCOUNT)) == 1  # nothing captured


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", [True, None, RuntimeError("stat failed")],
                         ids=["activity", "unknown", "probe-error"])
async def test_activity_or_doubt_samples(stores, answer):
    monitor, usage, clock = stores
    await _captured(usage, clock, ago=timedelta(minutes=5))
    runner, capture = _runner(monitor, clock, _Probe(answer))
    assert await runner.tick() is True
    assert capture.calls == 1
    state = await monitor.get(ACCOUNT)
    assert state.last_skipped_at is None and state.last_finished_at == clock.now


@pytest.mark.asyncio
@pytest.mark.parametrize("ago,expected", [
    (IDLE_FALLBACK - timedelta(seconds=1), 0), (IDLE_FALLBACK, 1), (IDLE_FALLBACK + timedelta(hours=5), 1),
], ids=["just-under", "exactly", "long-idle"])
async def test_idle_account_is_still_sampled_every_fallback_period(stores, ago, expected):
    """Other devices spend the same quota: an idle local machine cannot vouch for it."""
    monitor, usage, clock = stores
    await _captured(usage, clock, ago=ago)
    runner, capture = _runner(monitor, clock, _Probe(False))
    await runner.tick()
    assert capture.calls == expected
    assert IDLE_FALLBACK == timedelta(hours=2)


@pytest.mark.asyncio
async def test_a_passed_window_reset_is_sampled_even_when_idle(stores):
    monitor, usage, clock = stores
    await _captured(usage, clock, ago=timedelta(minutes=5), resets_in=timedelta(minutes=4))
    runner, capture = _runner(monitor, clock, _Probe(False))
    await runner.tick()
    assert capture.calls == 1


@pytest.mark.asyncio
async def test_a_last_capture_stamped_after_now_samples(stores):
    """The clock went back: journals written since carry times before the last capture."""
    monitor, usage, clock = stores
    await _captured(usage, clock, ago=-timedelta(minutes=10))
    runner, capture = _runner(monitor, clock, _Probe(False))
    await runner.tick()
    assert capture.calls == 1
    state = await monitor.get(ACCOUNT)
    assert state.last_skipped_at is None and state.last_finished_at == clock.now


@pytest.mark.asyncio
async def test_no_previous_capture_samples(stores):
    monitor, _usage, clock = stores
    probe = _Probe(False)
    runner, capture = _runner(monitor, clock, probe)
    await runner.tick()
    assert capture.calls == 1 and probe.since == []


@pytest.mark.asyncio
async def test_a_skip_keeps_the_previous_error_visible(stores):
    monitor, usage, clock = stores
    await _captured(usage, clock, ago=timedelta(minutes=5))
    claim = await monitor.claim_due("failing", clock.now)
    assert await monitor.finish(claim, None, [], error="原生账号采样超时，已安排下次重试。")
    clock.now += INTERVAL
    runner, capture = _runner(monitor, clock, _Probe(False))
    assert await runner.tick() is True
    state = await monitor.get(ACCOUNT)
    assert capture.calls == 0
    assert state.status == "error" and state.last_error.startswith("原生账号采样超时")
    assert state.last_skipped_at == clock.now


@pytest.mark.asyncio
async def test_without_a_probe_every_due_round_samples(stores):
    monitor, usage, clock = stores
    await _captured(usage, clock, ago=timedelta(minutes=5))
    capture = _Capture(clock)
    runner = AccountMonitorRunner(monitor, capture=capture, clock=lambda: clock.now)
    await runner.tick()
    assert capture.calls == 1


@pytest.mark.asyncio
async def test_the_api_lifespan_wires_the_codex_activity_probe(tmp_path, monkeypatch):
    """Guards the gate itself: the API's runner is constructed with the probe."""
    async def no_start(self):
        return None

    monkeypatch.setattr(AccountMonitorRunner, "start", no_start)
    monkeypatch.setattr(CodexUsageRecorder, "start", no_start)
    app = FastAPI()
    url = f"sqlite+aiosqlite:///{tmp_path / 'lifespan.db'}"
    async with account_monitor_lifecycle.account_monitor_lifespan(app, url) as runner:
        assert runner._activity_probe is codex_activity.active_since
    await engine_pool.get_engine(url).dispose()


def test_default_runner_has_no_gate():
    runner = AccountMonitorRunner(MonitorRepository("sqlite+aiosqlite:///:memory:"))
    assert runner._activity_probe is None
    assert monitor_module.IDLE_FALLBACK == timedelta(hours=2)
