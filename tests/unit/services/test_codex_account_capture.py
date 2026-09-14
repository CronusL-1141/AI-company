"""Protocol and lifecycle coverage without real account or model requests."""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest

from aiteam.services import codex_account_capture as capture

ACCOUNT_ID = "synthetic-account-a"
ACCOUNT = {"type": "chatgpt", "email": "synthetic@example.invalid", "planType": "pro"}
RESET = 1_800_000_000


def _window(used: Any = 20, minutes: Any = 10_080, reset: Any = RESET) -> dict[str, Any]:
    return {"usedPercent": used, "windowDurationMins": minutes, "resetsAt": reset}


def _limits() -> dict[str, Any]:
    return {
        "accountId": ACCOUNT_ID,
        "rateLimits": {"limitId": "legacy", "secondary": _window(99)},
        "rateLimitsByLimitId": {
            "codex": {"limitId": "codex", "primary": _window(3, 300), "secondary": _window(20)},
            "additional": {"limitId": "additional", "primary": _window(40)},
        },
    }


def _results() -> list[dict[str, Any] | None]:
    return [
        {}, {"account": deepcopy(ACCOUNT), "requiresOpenaiAuth": True},
        _limits(), _limits(), {"account": deepcopy(ACCOUNT), "requiresOpenaiAuth": True},
    ]


class _Input:
    def __init__(self, process: _Process) -> None:
        self.process = process
        self.closed = False

    def write(self, data: bytes) -> None:
        message = json.loads(data)
        expected = [
            ("initialize", {"clientInfo": {"name": "aiteam_account_quota", "version": "1.0.0"}}),
            ("initialized", {}),
            ("account/read", {"refreshToken": False}),
            ("account/rateLimits/read", {"excludeResetCreditDetails": True}),
            ("account/rateLimits/read", {"excludeResetCreditDetails": True}),
            ("account/read", {"refreshToken": False}),
        ]
        if self.process.include_activity:
            expected.insert(4, ("account/usage/read", {}))
        method, params = expected[len(self.process.messages)]
        assert message["method"] == method
        assert message["params"] == params
        self.process.messages.append(message)
        if "id" not in message:
            assert method == "initialized"
            return
        assert message["id"] == self.process.response_index + 1
        result = self.process.results[self.process.response_index]
        self.process.response_index += 1
        override = self.process.reply_overrides.get(message["id"])
        if override is not None:
            self.process.stdout.feed_data(json.dumps({"id": message["id"], **override}).encode() + b"\n")
            return
        if result is None:
            self.process.blocked.set()
            return
        self.process.stdout.feed_data(json.dumps({"id": message["id"], "result": result}).encode() + b"\n")

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True


class _Process:
    def __init__(self, results: list[dict[str, Any] | None], include_activity: bool = False) -> None:
        self.results = results
        self.include_activity = include_activity
        self.reply_overrides: dict[int, dict[str, Any]] = {}
        self.messages: list[dict[str, Any]] = []
        self.response_index = 0
        self.stdout = asyncio.StreamReader(limit=capture._MAX_RESPONSE_BYTES)
        self.stdin = _Input(self)
        self.returncode: int | None = None
        self.terminated = False
        self.killed = False
        self.waited = False
        self.exited = asyncio.Event()
        self.blocked = asyncio.Event()

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15
        self.exited.set()

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9
        self.exited.set()

    async def wait(self) -> int:
        self.waited = True
        await self.exited.wait()
        return self.returncode


def _install(monkeypatch: pytest.MonkeyPatch, results=None, include_activity: bool = False) -> _Process:
    process = _Process(_results() if results is None else results, include_activity=include_activity)
    monkeypatch.setenv("AITEAM_CODEX_BINARY", sys.executable)

    async def spawn(*args, **kwargs):
        assert args == (sys.executable, "app-server", "--stdio", "-c", "analytics.enabled=false")
        assert kwargs["stderr"] == asyncio.subprocess.DEVNULL
        assert kwargs["stdin"] == kwargs["stdout"] == asyncio.subprocess.PIPE
        assert kwargs["limit"] == capture._MAX_RESPONSE_BYTES
        return process

    monkeypatch.setattr(capture.asyncio, "create_subprocess_exec", spawn)
    return process


def _activity() -> dict[str, Any]:
    return {
        "summary": {"lifetimeTokens": 3_877_787_459},
        "dailyUsageBuckets": [{"startDate": "2026-09-13", "tokens": 1200}, {"startDate": "2026-09-14", "tokens": 3400}],
        "threadUsage": None,
    }


def _plan_results() -> list[dict[str, Any] | None]:
    result = _results()
    result.insert(3, _activity())
    return result


async def test_capture_uses_second_sample_and_does_not_persist_identity(monkeypatch):
    results = _results()
    results[3]["rateLimitsByLimitId"]["codex"]["secondary"]["usedPercent"] = 24
    process = _install(monkeypatch, results)
    times = []

    def fake_now():
        value = datetime(2026, 9, 14, tzinfo=UTC) + timedelta(seconds=len(times))
        times.append(value)
        return value

    monkeypatch.setattr(capture, "utc_now", fake_now)
    account, snapshots = await capture.capture_account()
    key = hashlib.sha256(ACCOUNT_ID.encode()).hexdigest()
    assert account.account_key == key
    assert account.label == f"Codex {key[-8:]}"
    assert account.created_at == times[3]
    assert {item.limit_id for item in snapshots} == {"codex", "additional"}
    assert {item.used_percent for item in snapshots} == {Decimal(24), Decimal(40)}
    assert all(item.observed_at == times[3] for item in snapshots)
    assert all(item.resets_at == datetime.fromtimestamp(RESET, UTC) for item in snapshots)
    assert all(item.window_duration_ms == 604_800_000 for item in snapshots)
    assert all(item.source == "codex_app_server" for item in snapshots)
    assert len({UUID(item.snapshot_id) for item in snapshots}) == 2
    serialized = account.model_dump_json() + "".join(item.model_dump_json() for item in snapshots)
    assert ACCOUNT_ID not in serialized and ACCOUNT["email"] not in serialized
    assert len(process.messages) == 6
    assert process.stdin.closed and process.terminated and process.waited


@pytest.mark.parametrize("change", ["quota_account", "visible_account", "missing_first_id", "missing_second_id"])
async def test_changed_or_unknown_account_discards_the_complete_sample(monkeypatch, change):
    results = _results()
    if change == "quota_account":
        results[3]["accountId"] = "synthetic-account-b"
    elif change == "visible_account":
        results[4]["account"]["email"] = "changed@example.invalid"
    else:
        results[2 if change == "missing_first_id" else 3].pop("accountId")
    process = _install(monkeypatch, results)
    with pytest.raises(capture.CodexAccountCaptureError, match="账号") as caught:
        await capture.capture_account()
    assert ACCOUNT_ID not in str(caught.value)
    assert process.terminated and process.waited


@pytest.mark.parametrize("buckets", [{}, None, {"codex": {"primary": _window(2, 300)}}])
async def test_empty_present_map_never_falls_back_to_legacy(monkeypatch, buckets):
    results = _results()
    results[3]["rateLimitsByLimitId"] = buckets
    process = _install(monkeypatch, results)
    with pytest.raises(capture.CodexAccountCaptureError, match="每周额度"):
        await capture.capture_account()
    assert process.waited


async def test_legacy_window_is_used_only_when_map_key_is_absent(monkeypatch):
    results = _results()
    results[3].pop("rateLimitsByLimitId")
    _install(monkeypatch, results)
    _, snapshots = await capture.capture_account()
    assert len(snapshots) == 1
    assert snapshots[0].limit_id == "legacy" and snapshots[0].used_percent == 99


@pytest.mark.parametrize("window", [
    _window(None), _window(True), _window(101), _window(-1),
    _window(10, reset=None), _window(10, reset=True), _window(10, reset=10**25),
])
async def test_invalid_weekly_measurement_is_not_silently_dropped(monkeypatch, window):
    results = _results()
    results[3]["rateLimitsByLimitId"]["codex"]["secondary"] = window
    _install(monkeypatch, results)
    with pytest.raises(capture.CodexAccountCaptureError, match="周额度"):
        await capture.capture_account()


@pytest.mark.parametrize("account", [None, {"type": "apiKey"}, {"type": "chatgpt", "planType": "pro"}])
async def test_unavailable_account_stops_before_quota_read(monkeypatch, account):
    results = _results()
    results[1]["account"] = account
    process = _install(monkeypatch, results)
    with pytest.raises(capture.CodexAccountCaptureError, match="账号"):
        await capture.capture_account()
    assert not any(message["method"] == "account/rateLimits/read" for message in process.messages)


@pytest.mark.parametrize("message", [
    {"id": 1, "error": {"message": "unauthorized refresh failed private@example.invalid synthetic-account-a"}},
    {"id": "server-request", "method": "account/chatgptAuthTokens/refresh", "params": {"secret": "never-display"}},
])
async def test_auth_errors_are_redacted_and_never_answered(monkeypatch, message):
    process = _install(monkeypatch, [None])
    process.stdout.feed_data(json.dumps(message).encode() + b"\n")
    with pytest.raises(capture.CodexAccountCaptureError, match="授权|刷新") as caught:
        await capture.capture_account()
    assert "private@example.invalid" not in str(caught.value)
    assert "never-display" not in str(caught.value)
    assert ACCOUNT_ID not in str(caught.value)
    assert len(process.messages) == 1 and process.waited


async def test_timeout_reaps_child(monkeypatch):
    process = _install(monkeypatch, [None])
    monkeypatch.setattr(capture, "_CAPTURE_TIMEOUT_SECONDS", 0.01)
    with pytest.raises(capture.CodexAccountCaptureError, match="超时"):
        await capture.capture_account()
    assert process.stdin.closed and process.terminated and process.waited


async def test_cancellation_reaps_child(monkeypatch):
    process = _install(monkeypatch, [None])
    task = asyncio.create_task(capture.capture_account())
    await asyncio.wait_for(process.blocked.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert process.stdin.closed and process.terminated and process.waited


async def test_cancellation_during_cleanup_waits_for_kill_and_reap(monkeypatch):
    process = _install(monkeypatch)
    cleanup_started = asyncio.Event()
    monkeypatch.setattr(capture, "_STOP_TIMEOUT_SECONDS", 0.05)

    def ignore_terminate():
        process.terminated = True
        cleanup_started.set()

    monkeypatch.setattr(process, "terminate", ignore_terminate)
    task = asyncio.create_task(capture.capture_account())
    await asyncio.wait_for(cleanup_started.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert process.killed and process.returncode == -9 and process.waited
    assert not any(
        pending.get_coro().__name__ == "_discard_stdout" for pending in asyncio.all_tasks()
    )


def test_missing_native_binary_does_not_use_terminal_path(monkeypatch, tmp_path):
    monkeypatch.delenv("AITEAM_CODEX_BINARY", raising=False)
    monkeypatch.setattr(capture.sys, "platform", "darwin")
    monkeypatch.setattr(capture, "_DESKTOP_BINARY", tmp_path / "absent")
    with pytest.raises(capture.CodexAccountCaptureError, match="可执行文件不可用"):
        capture._resolve_binary()


def test_other_platform_requires_explicit_binary(monkeypatch):
    monkeypatch.delenv("AITEAM_CODEX_BINARY", raising=False)
    monkeypatch.setattr(capture.sys, "platform", "linux")
    with pytest.raises(capture.CodexAccountCaptureError, match="未配置"):
        capture._resolve_binary()
    monkeypatch.setenv("AITEAM_CODEX_BINARY", sys.executable)
    assert str(capture._resolve_binary()) == sys.executable


@pytest.mark.parametrize("configured", ["", "codex", "relative/codex"])
def test_binary_override_requires_an_absolute_path(monkeypatch, configured):
    monkeypatch.setenv("AITEAM_CODEX_BINARY", configured)
    with pytest.raises(capture.CodexAccountCaptureError, match="绝对路径"):
        capture._resolve_binary()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signal behavior")
async def test_real_child_large_stderr_and_ignored_termination_are_bounded(monkeypatch):
    monkeypatch.setenv("AITEAM_CODEX_BINARY", sys.executable)
    monkeypatch.setattr(capture, "_CAPTURE_TIMEOUT_SECONDS", 0.3)
    monkeypatch.setattr(capture, "_STOP_TIMEOUT_SECONDS", 0.05)
    original_spawn = asyncio.create_subprocess_exec
    children = []
    script = (
        "import os, signal, sys\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "os.write(2, b'x' * 1000000)\n"
        "for line in sys.stdin:\n"
        "    pass\n"
        "signal.pause()\n"
    )

    async def spawn(*args, **kwargs):
        child = await original_spawn(sys.executable, "-u", "-c", script, **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(capture.asyncio, "create_subprocess_exec", spawn)
    with pytest.raises(capture.CodexAccountCaptureError, match="超时"):
        await capture.capture_account()
    assert len(children) == 1
    assert children[0].returncode == -9
    assert await children[0].wait() == -9


async def test_real_child_excess_stdout_is_discarded_during_reaping(monkeypatch):
    monkeypatch.setenv("AITEAM_CODEX_BINARY", sys.executable)
    monkeypatch.setattr(capture, "_MAX_RESPONSE_BYTES", 1_024)
    monkeypatch.setattr(capture, "_CAPTURE_TIMEOUT_SECONDS", 1)
    original_spawn = asyncio.create_subprocess_exec
    children = []

    async def spawn(*args, **kwargs):
        script = "import os, sys; os.write(1, b'x' * 1000000); sys.stdin.read()"
        child = await original_spawn(sys.executable, "-u", "-c", script, **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(capture.asyncio, "create_subprocess_exec", spawn)
    with pytest.raises(capture.CodexAccountCaptureError, match="无效数据|大小限制"):
        await capture.capture_account()
    assert len(children) == 1 and children[0].returncode is not None
    assert await children[0].wait() is not None


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signal behavior")
@pytest.mark.parametrize("cancel_count", [1, 2])
async def test_real_child_cancelled_cleanup_is_finished_before_return(monkeypatch, cancel_count):
    monkeypatch.setenv("AITEAM_CODEX_BINARY", sys.executable)
    monkeypatch.setattr(capture, "_STOP_TIMEOUT_SECONDS", 0.1)
    original_spawn = asyncio.create_subprocess_exec
    cleanup_started = asyncio.Event()
    children = []
    script = (
        "import json, signal, sys\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "results = json.loads(sys.argv[1])\n"
        "for line in sys.stdin:\n"
        "    request = json.loads(line)\n"
        "    if 'id' in request:\n"
        "        print(json.dumps({'id': request['id'], 'result': results.pop(0)}), flush=True)\n"
        "signal.pause()\n"
    )

    async def spawn(*args, **kwargs):
        child = await original_spawn(sys.executable, "-u", "-c", script, json.dumps(_results()), **kwargs)
        children.append(child)
        original_terminate = child.terminate

        def terminate_owned_child():
            original_terminate()
            cleanup_started.set()

        monkeypatch.setattr(child, "terminate", terminate_owned_child)
        return child

    monkeypatch.setattr(capture.asyncio, "create_subprocess_exec", spawn)
    task = asyncio.create_task(capture.capture_account())
    try:
        await asyncio.wait_for(cleanup_started.wait(), 2)
        for _ in range(cancel_count):
            task.cancel()
            await asyncio.sleep(0)
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 1)
        assert task.cancelled()
        assert len(children) == 1 and children[0].returncode == -9
        assert await asyncio.wait_for(children[0].wait(), 0.1) == -9
        assert not any(
            pending.get_coro().__name__ in {"_stop_process", "_discard_stdout"}
            for pending in asyncio.all_tasks()
        )
    finally:
        # Recovery is restricted to the exact child this test created.
        for child in children:
            if child.returncode is None:
                child.kill()
            await asyncio.wait_for(child.wait(), 1)
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_plan_capture_all_windows_share_ids_and_real_observation_times(monkeypatch):
    process = _install(monkeypatch, _plan_results(), include_activity=True)
    times = []

    def fake_now():
        instant = datetime(2026, 9, 14, tzinfo=UTC) + timedelta(seconds=len(times))
        times.append(instant)
        return instant

    monkeypatch.setattr(capture, "utc_now", fake_now)
    account, quotas, plans = await capture.capture_plan_account()
    assert len(quotas) == len(plans) == 3
    assert {plan.window_duration_ms for plan in plans} == {18_000_000, 604_800_000}
    for quota, plan in zip(quotas, plans, strict=True):
        for field in (
            "snapshot_id", "account_key", "limit_id", "window_duration_ms", "resets_at", "observed_at", "used_percent",
        ):
            assert getattr(quota, field) == getattr(plan, field)
        assert plan.account_key == account.account_key
        assert plan.observed_at == times[4]
        assert plan.activity_observed_at == times[3]
        assert plan.activity_tokens == 3_877_787_459
        assert plan.activity_scope == hashlib.sha256(b'["2026-09-13","2026-09-14"]').hexdigest()
    assert process.waited


@pytest.mark.parametrize("change", ["missing_days", "empty_days", "null_total", "bool_total", "oversized_total"])
async def test_plan_missing_activity_still_returns_percentages(monkeypatch, change):
    results = _plan_results()
    if change == "missing_days":
        results[3].pop("dailyUsageBuckets")
    elif change == "empty_days":
        results[3]["dailyUsageBuckets"] = []
    else:
        totals = {"null_total": None, "bool_total": True, "oversized_total": 2**53}
        results[3]["summary"]["lifetimeTokens"] = totals[change]
    _install(monkeypatch, results, include_activity=True)
    _, quotas, plans = await capture.capture_plan_account()
    assert len(quotas) == len(plans) == 3
    assert {plan.used_percent for plan in plans} == {3, 20, 40}
    assert all(
        plan.activity_tokens is plan.activity_scope is plan.activity_observed_at is None for plan in plans
    )


async def test_plan_scope_depends_on_date_set_and_keeps_measured_zero(monkeypatch):
    results = _plan_results()
    results[3]["summary"]["lifetimeTokens"] = 0
    results[3]["dailyUsageBuckets"].reverse()
    results[3]["dailyUsageBuckets"].append({"startDate": "2026-09-14", "tokens": 0})
    _install(monkeypatch, results, include_activity=True)
    _, _, plans = await capture.capture_plan_account()
    assert all(plan.activity_tokens == 0 for plan in plans)
    assert all(plan.activity_scope == hashlib.sha256(b'["2026-09-13","2026-09-14"]').hexdigest() for plan in plans)


async def test_plan_usage_unsupported_preserves_quota_bracket(monkeypatch):
    process = _install(monkeypatch, _plan_results(), include_activity=True)
    process.reply_overrides[4] = {"error": {"code": -32601, "message": "Unknown GetAccountTokenUsageParams"}}
    _, quotas, plans = await capture.capture_plan_account()
    assert len(quotas) == len(plans) == 3
    assert all(plan.activity_tokens is None for plan in plans)
    assert process.messages[-1]["method"] == "account/read" and process.waited


@pytest.mark.parametrize("failure", [
    {"method": "account/chatgptAuthTokens/refresh"},
    {"error": {"message": "unauthorized"}},
    {"error": {"code": 403, "message": "Denied"}},
])
async def test_plan_usage_auth_failure_is_fatal(monkeypatch, failure):
    process = _install(monkeypatch, _plan_results(), include_activity=True)
    process.reply_overrides[4] = failure
    with pytest.raises(capture.CodexAccountCaptureError, match="授权|刷新"):
        await capture.capture_plan_account()
    assert process.waited


@pytest.mark.parametrize("late_auth_error", [False, True])
async def test_plan_usage_timeout_ignores_only_non_auth_late_response(monkeypatch, late_auth_error):
    results = _plan_results()
    results[3] = None
    process = _install(monkeypatch, results, include_activity=True)
    monkeypatch.setattr(capture, "_USAGE_TIMEOUT_SECONDS", 0.01)
    original_write = process.stdin.write

    def inject_late_response(data):
        message = json.loads(data)
        if message["method"] == "account/rateLimits/read" and process.response_index == 4:
            late = {"id": 4, "error": {"message": "authentication failed"}} if late_auth_error else {
                "id": 4, "result": _activity(),
            }
            process.stdout.feed_data(json.dumps(late).encode() + b"\n")
        original_write(data)

    monkeypatch.setattr(process.stdin, "write", inject_late_response)
    if late_auth_error:
        with pytest.raises(capture.CodexAccountCaptureError, match="授权"):
            await capture.capture_plan_account()
    else:
        _, quotas, plans = await capture.capture_plan_account()
        assert len(quotas) == len(plans) == 3
        assert all(plan.activity_tokens is None for plan in plans)
    assert process.waited


@pytest.mark.parametrize("change", ["quota_account", "visible_account"])
async def test_plan_changed_account_discards_activity_and_quota(monkeypatch, change):
    results = _plan_results()
    if change == "quota_account":
        results[4]["accountId"] = "synthetic-account-b"
    else:
        results[5]["account"]["email"] = "changed@example.invalid"
    process = _install(monkeypatch, results, include_activity=True)
    with pytest.raises(capture.CodexAccountCaptureError, match="账号发生变化"):
        await capture.capture_plan_account()
    assert process.waited
