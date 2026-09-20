"""Runner lifecycle checks against real, isolated monitor persistence."""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
import pytest_asyncio

from aiteam.services import account_monitor as monitor_module
from aiteam.services.account_monitor import AccountMonitorRunner
from aiteam.services.codex_account_capture import CodexAccountCaptureError, _stop_process
from aiteam.storage import account_monitor as repository_module
from aiteam.storage.account_monitor import MonitorRepository
from aiteam.storage.account_usage import AccountUsageRepository
from aiteam.storage.engine_pool import engine_pool
from aiteam.types import PricingAccount, PricingMonitorSettings, PricingQuotaSnapshot

ACCOUNT = "a" * 64
OTHER_ACCOUNT = "b" * 64


@pytest_asyncio.fixture
async def stores(tmp_path, monkeypatch):
    clock = SimpleNamespace(now=datetime(2026, 9, 14, 5, 0, tzinfo=UTC))
    monkeypatch.setattr(repository_module, "utc_now", lambda: clock.now)
    monkeypatch.setattr(monitor_module, "_login_source_stamp", lambda: ("isolated-test-source",))
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'monitor.db'}"
    usage = AccountUsageRepository(database_url)
    monitor = MonitorRepository(database_url)
    await usage.init_db()
    await monitor.init_db()
    for key in (ACCOUNT, OTHER_ACCOUNT):
        await usage.upsert_account(PricingAccount(account_key=key, label="User alias", created_at=clock.now))
    yield monitor, usage, clock
    await engine_pool.get_engine(database_url).dispose()


def _capture_result(clock, key=ACCOUNT):
    return PricingAccount(account_key=key, label="Native alias", created_at=clock.now), [
        PricingQuotaSnapshot(
            snapshot_id=str(uuid4()), account_key=key, limit_id="codex", used_percent=Decimal(25),
            window_duration_ms=604800000, resets_at=clock.now + timedelta(days=3),
            observed_at=clock.now, source="codex_app_server",
        ),
    ]


async def _enable(repository):
    await repository.configure(ACCOUNT, PricingMonitorSettings(enabled=True, interval_ms=300000))


@pytest.mark.asyncio
async def test_start_confirms_current_account_before_creating_default_monitor(stores, monkeypatch):
    repository, usage, clock = stores
    saved = asyncio.Event()
    original = repository.save_source_capture

    async def save(*args, **kwargs):
        result = await original(*args, **kwargs)
        saved.set()
        return result

    monkeypatch.setattr(repository, "save_source_capture", save)

    async def capture():
        return _capture_result(clock, OTHER_ACCOUNT)

    runner = AccountMonitorRunner(repository, capture=capture, clock=lambda: clock.now)
    assert not runner.is_running
    await runner.start()
    await runner.start()
    try:
        await asyncio.wait_for(saved.wait(), timeout=1)
        assert runner.is_running
        assert await usage.list_snapshots(ACCOUNT) == []
        assert not (await repository.get(ACCOUNT)).settings.enabled
        confirmed = await repository.get(OTHER_ACCOUNT)
        assert confirmed.settings.enabled and confirmed.revision == 1
        assert confirmed.next_run_at == clock.now + timedelta(minutes=30)
        assert len(await usage.list_snapshots(OTHER_ACCOUNT)) == 1
    finally:
        await runner.stop()
    assert not runner.is_running


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.asyncio
async def test_bootstrap_and_restart_preserve_saved_monitor_settings(stores, enabled):
    repository, usage, clock = stores
    saved = await repository.configure(
        ACCOUNT, PricingMonitorSettings(enabled=enabled, interval_ms=300000),
    )

    async def capture():
        return _capture_result(clock)

    for _ in range(2):
        clock.now += timedelta(seconds=10)
        runner = AccountMonitorRunner(repository, capture=capture, clock=lambda: clock.now)
        assert await runner.bootstrap()
        assert not await runner.bootstrap()
        expected = saved.model_copy(update={"last_finished_at": clock.now}) if enabled else saved
        assert await MonitorRepository(repository._db_url).get(ACCOUNT) == expected
    assert len(await usage.list_snapshots(ACCOUNT)) == 2


@pytest.mark.asyncio
async def test_bootstrap_retries_are_bounded_and_do_not_create_false_enabled_state(stores, caplog):
    repository, usage, clock = stores
    calls = 0

    async def capture():
        nonlocal calls
        calls += 1
        raise OSError("private-source-person@example.test")

    runner = AccountMonitorRunner(repository, capture=capture, clock=lambda: clock.now)
    for _ in range(3):
        assert await runner.bootstrap()
        assert not await runner.bootstrap()
        clock.now += timedelta(seconds=30)
    assert not await runner.bootstrap()
    assert calls == 3
    state = await repository.get(ACCOUNT)
    assert not state.settings.enabled and state.revision == 0 and state.next_run_at is None
    assert await usage.list_snapshots(ACCOUNT) == []
    assert "OSError" in caplog.text and "private-source" not in caplog.text
    claim = await repository.claim_source("after-failures", clock.now)
    assert claim is not None
    await repository.release_source(claim)


@pytest.mark.asyncio
async def test_concurrent_bootstrap_uses_one_native_source_and_explicit_pause_wins(stores):
    repository, usage, clock = stores
    started, complete = asyncio.Event(), asyncio.Event()
    calls = 0

    async def capture():
        nonlocal calls
        calls += 1
        started.set()
        await complete.wait()
        return _capture_result(clock)

    first = AccountMonitorRunner(repository, capture=capture, clock=lambda: clock.now)
    second = AccountMonitorRunner(
        MonitorRepository(repository._db_url), capture=capture, clock=lambda: clock.now,
    )
    work = asyncio.create_task(first.bootstrap())
    await asyncio.wait_for(started.wait(), timeout=1)
    assert not await second.bootstrap()
    paused = await repository.configure(ACCOUNT, PricingMonitorSettings(enabled=False))
    complete.set()
    assert await asyncio.wait_for(work, timeout=1)
    assert calls == 1 and await repository.get(ACCOUNT) == paused
    assert len(await usage.list_snapshots(ACCOUNT)) == 1


@pytest.mark.asyncio
async def test_bootstrap_timeout_reaps_collector_without_defaulting_account(stores, monkeypatch):
    repository, usage, clock = stores
    monkeypatch.setattr(monitor_module, "_CAPTURE_DEADLINE_SECONDS", 0.01)
    reaped = asyncio.Event()

    async def capture():
        try:
            await asyncio.Future()
        finally:
            reaped.set()

    runner = AccountMonitorRunner(repository, capture=capture, clock=lambda: clock.now)
    assert await runner.bootstrap()
    assert reaped.is_set() and not (await repository.get(ACCOUNT)).settings.enabled
    assert await usage.list_snapshots(ACCOUNT) == []
    claim = await repository.claim_source("after-timeout", clock.now)
    assert claim is not None
    await repository.release_source(claim)


@pytest.mark.parametrize("source_change", [False, True])
@pytest.mark.asyncio
async def test_native_wrapper_enforces_source_fence_before_default_on(stores, monkeypatch, tmp_path, source_change):
    repository, usage, clock = stores
    source = (tmp_path, "stable-source", ((1, 2, 3), None))
    current = [source]
    monkeypatch.setattr(monitor_module, "_sample_source", lambda: current[0])

    async def capture(*, repository):
        if source_change:
            current[0] = (tmp_path, "changed-source", ((4, 5, 6), None))
        return _capture_result(clock)

    monkeypatch.setattr(monitor_module, "capture_local_plan_account", capture)
    runner = AccountMonitorRunner(repository, clock=lambda: clock.now)
    assert await runner.bootstrap()
    state = await repository.get(ACCOUNT)
    assert state.settings.enabled is not source_change
    assert len(await usage.list_snapshots(ACCOUNT)) == (0 if source_change else 1)


@pytest.mark.asyncio
async def test_native_wrapper_missing_source_never_calls_native_capture(stores, monkeypatch):
    repository, usage, clock = stores

    def missing_source():
        raise OSError("source unavailable")

    async def unexpected_capture(**kwargs):
        pytest.fail("an unknown source cannot bootstrap account monitoring")

    monkeypatch.setattr(monitor_module, "_sample_source", missing_source)
    monkeypatch.setattr(monitor_module, "capture_local_plan_account", unexpected_capture)
    runner = AccountMonitorRunner(repository, clock=lambda: clock.now)
    assert await runner.bootstrap()
    assert not (await repository.get(ACCOUNT)).settings.enabled
    assert await usage.list_snapshots(ACCOUNT) == []


@pytest.mark.asyncio
async def test_due_tick_persists_once_and_preserves_account_alias(stores):
    repository, usage, clock = stores
    calls = 0

    async def capture():
        nonlocal calls
        calls += 1
        return _capture_result(clock)

    await _enable(repository)
    runner = AccountMonitorRunner(repository, capture=capture, clock=lambda: clock.now)
    assert await runner.tick()
    assert not await runner.tick()
    assert calls == 1
    assert len(await usage.list_snapshots(ACCOUNT)) == 1
    assert (await usage.get_account(ACCOUNT)).label == "User alias"
    state = await repository.get(ACCOUNT)
    assert state.status == "waiting"
    assert state.next_run_at == clock.now + timedelta(minutes=5)


@pytest.mark.asyncio
async def test_thirty_second_interval_is_due_only_after_completion_plus_period(stores):
    repository, usage, clock = stores
    calls = 0

    async def capture():
        nonlocal calls
        calls += 1
        return _capture_result(clock)

    await repository.configure(ACCOUNT, PricingMonitorSettings(enabled=True, interval_ms=30000))
    runner = AccountMonitorRunner(repository, capture=capture, clock=lambda: clock.now)
    assert await runner.tick() is True
    assert (await repository.get(ACCOUNT)).next_run_at == clock.now + timedelta(seconds=30)
    clock.now += timedelta(seconds=29)
    assert await runner.tick() is False
    clock.now += timedelta(seconds=1)
    assert await runner.tick() is True
    assert calls == 2
    assert len(await usage.list_snapshots(ACCOUNT)) == 2


@pytest.mark.asyncio
async def test_restart_runs_one_overdue_sample_without_catchup(stores):
    repository, usage, clock = stores
    calls = 0

    async def capture():
        nonlocal calls
        calls += 1
        return _capture_result(clock)

    await _enable(repository)
    clock.now += timedelta(days=4)
    runner = AccountMonitorRunner(repository, capture=capture, clock=lambda: clock.now)
    assert await runner.tick()
    restarted = AccountMonitorRunner(repository, capture=capture, clock=lambda: clock.now)
    assert not await restarted.tick()
    assert calls == 1 and len(await usage.list_snapshots(ACCOUNT)) == 1
    assert (await repository.get(ACCOUNT)).next_run_at == clock.now + timedelta(minutes=5)


@pytest.mark.asyncio
async def test_other_native_account_pauses_without_saving_either_account(stores):
    repository, usage, clock = stores

    async def capture():
        return _capture_result(clock, OTHER_ACCOUNT)

    await _enable(repository)
    runner = AccountMonitorRunner(repository, capture=capture, clock=lambda: clock.now)
    assert await runner.tick()
    state = await repository.get(ACCOUNT)
    assert state.status == "paused_account_changed" and state.next_run_at is None
    assert "绑定账号不一致" in state.last_error
    assert await usage.list_snapshots(ACCOUNT) == []
    assert await usage.list_snapshots(OTHER_ACCOUNT) == []
    assert not await runner.tick()


@pytest.mark.asyncio
async def test_monitor_account_switch_is_reconfirmed_before_defaulting_new_account(stores):
    repository, usage, clock = stores

    async def capture():
        return _capture_result(clock, OTHER_ACCOUNT)

    await _enable(repository)
    runner = AccountMonitorRunner(repository, capture=capture, clock=lambda: clock.now)
    assert await runner.tick()
    assert await usage.list_snapshots(OTHER_ACCOUNT) == []
    assert await runner.bootstrap()
    previous, current = await repository.get(ACCOUNT), await repository.get(OTHER_ACCOUNT)
    assert previous.status == "paused_account_changed" and previous.next_run_at is None
    assert previous.settings.enabled is False
    assert current.settings.enabled and current.next_run_at == clock.now + timedelta(minutes=30)
    assert await usage.list_snapshots(ACCOUNT) == []
    assert len(await usage.list_snapshots(OTHER_ACCOUNT)) == 1


@pytest.mark.parametrize("message", [
    "账号授权读取失败或需要刷新，当前只读采集不支持。",
    "当前原生 Codex 未提供已登录的 ChatGPT 账号。",
    "采样期间账号发生变化，此次额度未保存，请重试。",
])
@pytest.mark.asyncio
async def test_authentication_and_mid_capture_account_changes_pause(stores, message):
    repository, usage, clock = stores

    async def capture():
        raise CodexAccountCaptureError(message)

    await _enable(repository)
    runner = AccountMonitorRunner(repository, capture=capture, clock=lambda: clock.now)
    assert await runner.tick()
    state = await repository.get(ACCOUNT)
    assert state.status == "paused_account_changed" and state.next_run_at is None
    assert "自动恢复" in state.last_error
    assert await usage.list_snapshots(ACCOUNT) == []
    assert not await runner.tick()


@pytest.mark.asyncio
async def test_network_failure_is_bounded_redacted_and_scheduled_later(stores):
    repository, usage, clock = stores
    calls = 0

    async def capture():
        nonlocal calls
        calls += 1
        raise OSError("private-upstream-response-person@example.test")

    await _enable(repository)
    runner = AccountMonitorRunner(repository, capture=capture, clock=lambda: clock.now)
    assert await runner.tick()
    assert not await runner.tick()
    state = await repository.get(ACCOUNT)
    assert state.status == "error" and state.next_run_at == clock.now + timedelta(minutes=5)
    assert "private" not in state.last_error and "example.test" not in state.last_error
    assert calls == 1 and await usage.list_snapshots(ACCOUNT) == []


@pytest.mark.asyncio
async def test_stop_waits_for_collector_child_exit_and_releases_source(stores):
    repository, usage, clock = stores
    started = asyncio.Event()
    reaped = asyncio.Event()
    process = None

    async def capture():
        nonlocal process
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-B", "-c", "import sys; sys.stdin.buffer.read()",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        started.set()
        try:
            await asyncio.Future()
        finally:
            await _stop_process(process)
            reaped.set()

    await _enable(repository)
    runner = AccountMonitorRunner(repository, capture=capture, clock=lambda: clock.now)
    await runner.start()
    await asyncio.wait_for(started.wait(), timeout=1)
    await asyncio.wait_for(runner.stop(), timeout=2)
    assert reaped.is_set() and process.returncode is not None
    assert not runner.is_running
    assert await usage.list_snapshots(ACCOUNT) == []
    source = await repository.claim_source("manual-after-stop", clock.now)
    assert source is not None
    await repository.release_source(source)


@pytest.mark.asyncio
async def test_disable_cancels_collection_but_runner_can_resume(stores, monkeypatch):
    repository, usage, clock = stores
    started = asyncio.Event()
    cancelled = asyncio.Event()
    completed = asyncio.Event()
    calls = 0
    original_finish = repository.finish

    async def finish(*args, **kwargs):
        result = await original_finish(*args, **kwargs)
        completed.set()
        return result

    monkeypatch.setattr(repository, "finish", finish)

    async def capture():
        nonlocal calls
        calls += 1
        if calls == 1:
            started.set()
            try:
                await asyncio.Future()
            finally:
                cancelled.set()
        return _capture_result(clock)

    await _enable(repository)
    runner = AccountMonitorRunner(repository, capture=capture, clock=lambda: clock.now)
    await runner.start()
    try:
        await asyncio.wait_for(started.wait(), timeout=1)
        await repository.configure(ACCOUNT, PricingMonitorSettings(enabled=False))
        await runner.settings_changed(ACCOUNT)
        assert cancelled.is_set() and runner.is_running
        assert await usage.list_snapshots(ACCOUNT) == []
        await _enable(repository)
        await runner.settings_changed(ACCOUNT)
        await asyncio.wait_for(completed.wait(), timeout=1)
        assert calls == 2 and len(await usage.list_snapshots(ACCOUNT)) == 1
    finally:
        await runner.stop()


@pytest.mark.asyncio
async def test_cross_worker_settings_change_is_detected_on_renew(stores, monkeypatch):
    repository, usage, clock = stores
    monkeypatch.setattr(monitor_module, "_RENEW_SECONDS", 0.001)
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def capture():
        started.set()
        try:
            await asyncio.Future()
        finally:
            cancelled.set()

    await _enable(repository)
    runner = AccountMonitorRunner(repository, capture=capture, clock=lambda: clock.now)
    work = asyncio.create_task(runner.tick())
    await asyncio.wait_for(started.wait(), timeout=1)
    await repository.configure(ACCOUNT, PricingMonitorSettings(enabled=False))
    assert await asyncio.wait_for(work, timeout=1)
    assert cancelled.is_set() and await usage.list_snapshots(ACCOUNT) == []


@pytest.mark.asyncio
async def test_finish_fencing_blocks_disable_after_capture_completed(stores, monkeypatch):
    repository, usage, clock = stores
    original_finish = repository.finish

    async def finish(claim, account, snapshots, **kwargs):
        await repository.configure(ACCOUNT, PricingMonitorSettings(enabled=False))
        return await original_finish(claim, account, snapshots, **kwargs)

    monkeypatch.setattr(repository, "finish", finish)

    async def capture():
        return _capture_result(clock)

    await _enable(repository)
    runner = AccountMonitorRunner(repository, capture=capture, clock=lambda: clock.now)
    assert await runner.tick()
    assert await usage.list_snapshots(ACCOUNT) == []
    assert (await repository.get(ACCOUNT)).status == "disabled"


@pytest.mark.asyncio
async def test_capture_timeout_cancels_and_records_next_retry(stores, monkeypatch):
    repository, usage, clock = stores
    monkeypatch.setattr(monitor_module, "_CAPTURE_DEADLINE_SECONDS", 0.005)
    cancelled = asyncio.Event()

    async def capture():
        try:
            await asyncio.Future()
        finally:
            cancelled.set()

    await _enable(repository)
    runner = AccountMonitorRunner(repository, capture=capture, clock=lambda: clock.now)
    assert await runner.tick()
    state = await repository.get(ACCOUNT)
    assert cancelled.is_set()
    assert state.status == "error" and "超时" in state.last_error
    assert state.next_run_at == clock.now + timedelta(minutes=5)
    assert await usage.list_snapshots(ACCOUNT) == []


@pytest.mark.asyncio
async def test_expired_lease_cannot_commit_a_successful_capture(stores):
    repository, usage, clock = stores

    async def capture():
        clock.now += timedelta(seconds=61)
        return _capture_result(clock)

    await _enable(repository)
    runner = AccountMonitorRunner(repository, capture=capture, clock=lambda: clock.now)
    assert await runner.tick()
    assert await usage.list_snapshots(ACCOUNT) == []
    assert (await repository.get(ACCOUNT)).status == "waiting"


@pytest.mark.asyncio
async def test_round_timeout_after_capture_records_failure_without_saving_result(stores, monkeypatch, caplog):
    repository, usage, clock = stores
    monkeypatch.setattr(monitor_module, "_ROUND_DEADLINE_SECONDS", 0.05)

    async def capture():
        return _capture_result(clock)

    async def stalled_renewal(*args, **kwargs):
        await asyncio.Future()

    monkeypatch.setattr(repository, "renew_claim", stalled_renewal)
    await _enable(repository)
    runner = AccountMonitorRunner(repository, capture=capture, clock=lambda: clock.now)
    assert await asyncio.wait_for(runner.tick(), timeout=1)
    state = await repository.get(ACCOUNT)
    assert state.status == "error"
    assert "轮次超时" in state.last_error
    assert state.last_finished_at == clock.now
    assert state.next_run_at == clock.now + timedelta(minutes=5)
    assert await usage.list_snapshots(ACCOUNT) == []
    assert "TimeoutError" in caplog.text


@pytest.mark.parametrize("recovery_failure", ["exception", "timeout"])
@pytest.mark.asyncio
async def test_round_timeout_database_recovery_is_bounded_and_redacted(
    stores, monkeypatch, caplog, recovery_failure,
):
    repository, usage, clock = stores
    monkeypatch.setattr(monitor_module, "_ROUND_DEADLINE_SECONDS", 0.01)
    monkeypatch.setattr(monitor_module, "_RELEASE_DEADLINE_SECONDS", 0.01)

    async def capture():
        return _capture_result(clock)

    async def stalled_renewal(*args, **kwargs):
        await asyncio.Future()

    async def failed_finish(*args, **kwargs):
        if recovery_failure == "exception":
            raise OSError("private-db-response-person@example.test")
        await asyncio.Future()

    monkeypatch.setattr(repository, "renew_claim", stalled_renewal)
    monkeypatch.setattr(repository, "finish", failed_finish)
    await _enable(repository)
    runner = AccountMonitorRunner(repository, capture=capture, clock=lambda: clock.now)
    assert await asyncio.wait_for(runner.tick(), timeout=1)
    state = await repository.get(ACCOUNT)
    assert state.status == "sampling" and state.last_finished_at is None
    assert await usage.list_snapshots(ACCOUNT) == []
    assert "finalization failed" in caplog.text
    assert ("OSError" if recovery_failure == "exception" else "TimeoutError") in caplog.text
    assert "private-db" not in caplog.text and "example.test" not in caplog.text


@pytest.mark.asyncio
async def test_round_timeout_does_not_write_when_lease_has_expired(stores, monkeypatch, caplog):
    repository, usage, clock = stores
    monkeypatch.setattr(monitor_module, "_ROUND_DEADLINE_SECONDS", 0.01)

    async def capture():
        return _capture_result(clock)

    async def stalled_expired_renewal(*args, **kwargs):
        clock.now += timedelta(seconds=61)
        await asyncio.Future()

    monkeypatch.setattr(repository, "renew_claim", stalled_expired_renewal)
    await _enable(repository)
    runner = AccountMonitorRunner(repository, capture=capture, clock=lambda: clock.now)
    assert await asyncio.wait_for(runner.tick(), timeout=1)
    state = await repository.get(ACCOUNT)
    assert state.status == "sampling" and state.last_error is None and state.last_finished_at is None
    assert await usage.list_snapshots(ACCOUNT) == []
    assert "LeaseLost" in caplog.text


@pytest.mark.asyncio
async def test_login_source_changes_reuse_accounts_and_keep_manual_disable(stores, monkeypatch):
    repository, usage, clock = stores
    current = [ACCOUNT]
    stamp = [0]
    calls = []
    monkeypatch.setattr(monitor_module, "_login_source_stamp", lambda: (stamp[0],))

    async def capture():
        calls.append(current[0])
        return _capture_result(clock, current[0])

    await repository.configure(ACCOUNT, PricingMonitorSettings(enabled=True, interval_ms=300000))
    runner = AccountMonitorRunner(repository, capture=capture, clock=lambda: clock.now)
    for key in (ACCOUNT, OTHER_ACCOUNT, ACCOUNT):
        current[0] = key
        stamp[0] += 1
        clock.now += timedelta(seconds=10)
        assert await runner.bootstrap()
        assert runner.current_account_key == key
        assert not await runner.bootstrap()
        saved = await MonitorRepository(repository._db_url).get(key)
        assert saved.settings.enabled
    assert calls == [ACCOUNT, OTHER_ACCOUNT, ACCOUNT]
    assert (await repository.get(ACCOUNT)).settings.interval_ms == 300000
    assert len(await usage.list_snapshots(ACCOUNT)) == 2
    assert len(await usage.list_snapshots(OTHER_ACCOUNT)) == 1
    assert (await usage.get_account(ACCOUNT)).label == "User alias"
    before = await repository.configure(ACCOUNT, PricingMonitorSettings(enabled=False))
    for key in (OTHER_ACCOUNT, ACCOUNT):
        current[0] = key
        stamp[0] += 1
        assert await runner.bootstrap()
    assert await MonitorRepository(repository._db_url).get(ACCOUNT) == before
    assert not await runner.tick()


@pytest.mark.asyncio
async def test_logout_relogin_recovers_paused_same_account(stores, monkeypatch):
    repository, usage, clock = stores
    stamp = [0]
    logged_in = [True]
    monkeypatch.setattr(monitor_module, "_login_source_stamp", lambda: (stamp[0],))

    async def capture():
        if not logged_in[0]:
            raise CodexAccountCaptureError("当前原生 Codex 未提供已登录的 ChatGPT 账号。")
        return _capture_result(clock)

    await _enable(repository)
    runner = AccountMonitorRunner(repository, capture=capture, clock=lambda: clock.now)
    assert await runner.bootstrap()
    assert runner.current_account_key == ACCOUNT
    logged_in[0] = False
    stamp[0] += 1
    assert await runner.bootstrap()
    assert runner.current_account_key is None
    assert await runner.tick()
    assert (await repository.get(ACCOUNT)).status == "paused_account_changed"
    logged_in[0] = True
    stamp[0] += 1
    assert await runner.bootstrap()
    resumed = await MonitorRepository(repository._db_url).get(ACCOUNT)
    assert resumed.status == "waiting" and resumed.settings.enabled
    assert resumed.settings.interval_ms == 300000
    assert resumed.last_error is None
    assert runner.current_account_key == ACCOUNT
    assert len(await usage.list_snapshots(ACCOUNT)) == 2


@pytest.mark.asyncio
async def test_exhausted_discovery_retries_slowly_and_recovers_without_file_change(stores, monkeypatch):
    repository, usage, clock = stores
    monkeypatch.setattr(monitor_module, "_login_source_stamp", lambda: ("same",))
    calls = 0

    async def capture():
        nonlocal calls
        calls += 1
        if calls <= 4:
            raise OSError("temporary native failure")
        return _capture_result(clock)

    runner = AccountMonitorRunner(repository, capture=capture, clock=lambda: clock.now)
    for seconds in (0, 30, 30, 300, 300):
        clock.now += timedelta(seconds=seconds)
        assert await runner.bootstrap()
        assert not await runner.bootstrap()
    assert calls == 5
    assert runner.current_account_key == ACCOUNT
    assert (await repository.get(ACCOUNT)).settings.enabled
    assert len(await usage.list_snapshots(ACCOUNT)) == 1
    clock.now += timedelta(hours=1)
    assert not await runner.bootstrap()


def test_login_source_stamp_detects_replace_remove_without_reading_contents(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    auth = tmp_path / "auth.json"
    auth.write_text("do not inspect")
    original = monitor_module._login_source_stamp()

    def forbid(*args, **kwargs):
        pytest.fail("login discovery must not read authentication/configuration contents")

    monkeypatch.setattr(monitor_module.Path, "read_text", forbid)
    monkeypatch.setattr(monitor_module.Path, "read_bytes", forbid)
    assert monitor_module._login_source_stamp() == original
    replacement = tmp_path / "replacement"
    replacement.write_text("new login")
    replacement.replace(auth)
    replaced = monitor_module._login_source_stamp()
    assert replaced != original
    auth.unlink()
    assert monitor_module._login_source_stamp() != replaced
