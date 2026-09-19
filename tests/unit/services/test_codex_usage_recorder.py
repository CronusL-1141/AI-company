"""Independent default recording uses only synthetic logs and selected databases."""

import asyncio
import threading
from datetime import UTC, datetime

import pytest
from fastapi import FastAPI

from aiteam.api import account_monitor_lifecycle as lifecycle
from aiteam.services import codex_usage_recorder as recorder_module
from aiteam.services.codex_usage_recorder import CodexUsageRecorder
from aiteam.storage.account_monitor import MonitorRepository
from aiteam.storage.account_usage import AccountUsageRepository
from aiteam.storage.codex_usage_journal import CodexUsageJournalRepository
from aiteam.storage.engine_pool import engine_pool
from aiteam.types import PricingAccount, PricingMonitorSettings
from unit.services.test_codex_usage_journal import encode, ledger, source


@pytest.fixture
async def store(tmp_path):
    url = f"sqlite+aiosqlite:///{tmp_path / 'recorder.db'}"
    repository = CodexUsageJournalRepository(url)
    await repository.init_db()
    yield repository, url
    await engine_pool.get_engine(url).dispose()


async def test_no_account_or_prediction_setting_is_needed_and_restart_is_idempotent(store, tmp_path):
    repository, url = store
    source(tmp_path, encode(ledger(model_provider="unknown-provider")))
    recorder = CodexUsageRecorder(repository, codex_home=tmp_path)
    try:
        assert (await recorder.run_once())["observations_seen"] == 1
        assert await repository.count_observations() == 1
    finally:
        await recorder.stop()
    await engine_pool.get_engine(url).dispose()
    restarted = CodexUsageRecorder(CodexUsageJournalRepository(url), codex_home=tmp_path)
    try:
        await restarted.run_once()
        assert await repository.count_observations() == 1
        fact = (await repository.list_observations())[0]
        assert fact.model is None and fact.provider == "unknown-provider"
    finally:
        await restarted.stop()


async def test_small_file_budget_is_fair_across_ticks(store, tmp_path):
    repository, _ = store
    first = source(tmp_path, encode(ledger("first")))
    for number in range(8):
        (first.parent / f"{number}.jsonl").write_bytes(encode(ledger(str(number))))
    recorder = CodexUsageRecorder(repository, codex_home=tmp_path, max_files=1)
    try:
        for _ in range(12):
            await recorder.run_once()
        assert await repository.count_observations() == 9
    finally:
        await recorder.stop()


async def test_explicitly_paused_prediction_does_not_disable_fact_recording(store, tmp_path):
    repository, url = store
    accounts, monitor = AccountUsageRepository(url), MonitorRepository(url)
    await accounts.init_db()
    await monitor.init_db()
    key = "a" * 64
    await accounts.upsert_account(PricingAccount(
        account_key=key, label="Synthetic", created_at=datetime(2026, 9, 15, tzinfo=UTC),
    ))
    await monitor.configure(key, PricingMonitorSettings(enabled=False))
    source(tmp_path, encode(ledger()))
    recorder = CodexUsageRecorder(repository, codex_home=tmp_path)
    try:
        await recorder.run_once()
        assert await repository.count_observations() == 1
        assert (await monitor.get(key)).settings.enabled is False
    finally:
        await recorder.stop()


async def test_start_is_single_and_file_scanning_stays_off_event_loop(store, tmp_path, monkeypatch):
    repository, _ = store
    source(tmp_path, encode(ledger()))
    original = recorder_module.scan_journal_file
    scanned = threading.Event()
    event_loop_thread = threading.get_ident()

    def check_thread(*args, **kwargs):
        assert threading.get_ident() != event_loop_thread
        result = original(*args, **kwargs)
        scanned.set()
        return result

    monkeypatch.setattr(recorder_module, "scan_journal_file", check_thread)
    recorder = CodexUsageRecorder(repository, codex_home=tmp_path, interval_seconds=0.01)
    try:
        await recorder.start()
        task = recorder._task
        await recorder.start()
        assert recorder._task is task and recorder.is_running
        for _ in range(100):
            if await repository.count_observations() == 1:
                break
            await asyncio.sleep(0.01)
        assert scanned.is_set() and await repository.count_observations() == 1
    finally:
        await recorder.stop()
    assert not recorder.is_running


async def test_lifespan_owns_both_runners_with_same_explicit_database(store, tmp_path, monkeypatch):
    repository, url = store
    source(tmp_path, encode(ledger()))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    calls = []

    class Monitor:
        def __init__(self, repo):
            assert repo._db_url == url

        async def start(self):
            calls.append("monitor_started")

        async def stop(self):
            calls.append("monitor_stopped")

    monkeypatch.setattr(lifecycle, "AccountMonitorRunner", Monitor)
    app = FastAPI()
    async with lifecycle.account_monitor_lifespan(app, url):
        recorder = app.state.codex_usage_recorder
        assert recorder.is_running
        await recorder.run_once()
        assert await repository.count_observations() == 1
        assert calls == ["monitor_started"]
    assert calls == ["monitor_started", "monitor_stopped"]
    assert not recorder.is_running
    assert app.state.codex_usage_recorder is None and app.state.account_monitor_runner is None


async def test_recorder_start_failure_does_not_stop_existing_monitor(store, tmp_path, monkeypatch):
    _, url = store
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    calls = []

    async def fail_start(self):
        raise RuntimeError("synthetic startup failure")

    class Monitor:
        def __init__(self, repo):
            pass

        async def start(self):
            calls.append("started")

        async def stop(self):
            calls.append("stopped")

    monkeypatch.setattr(CodexUsageRecorder, "start", fail_start)
    monkeypatch.setattr(lifecycle, "AccountMonitorRunner", Monitor)
    app = FastAPI()
    async with lifecycle.account_monitor_lifespan(app, url):
        assert calls == ["started"]
        assert app.state.codex_usage_recorder.last_error == "RuntimeError"
    assert calls == ["started", "stopped"]
