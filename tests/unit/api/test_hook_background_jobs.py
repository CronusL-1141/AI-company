"""hook 后台作业的调度语义：single-flight、恰好补一轮、有界 drain、关停先 drain 再关库。

后台化解决的是"hook 响应等解析"，引入的是另一类风险 —— 作业被回收、脏标记被吞、
关停时写到已关闭的库、或者 drain 无界把重启拖进 SIGKILL。每一条都在这里单独钉。
"""

from __future__ import annotations

import asyncio
import gc
import inspect
import logging
import re
from datetime import timedelta
from pathlib import Path

import pytest

from aiteam.api import deps, hook_translator, leader_usage
from aiteam.api.hook_translator import BACKGROUND_DRAIN_TIMEOUT_SECONDS, _KeyedSingleFlight
from aiteam.api.routes import system
from aiteam.clock import utc_now

# ============================================================
# single-flight 与补跑
# ============================================================


@pytest.mark.asyncio
async def test_in_flight_key_coalesces_into_exactly_one_rerun_with_the_latest_job():
    runner = _KeyedSingleFlight()
    release = asyncio.Event()
    ran: list[str] = []

    def job(tag: str):
        async def run() -> None:
            ran.append(tag)
            if tag == "first":
                await release.wait()
        return run

    assert runner.submit(("k", "s"), job("first")) is True
    await asyncio.sleep(0)
    assert runner.submit(("k", "s"), job("second")) is False
    assert runner.submit(("k", "s"), job("third")) is False
    release.set()
    assert await runner.drain(timeout=5)
    assert ran == ["first", "third"], "脏标记要恰好补一轮，且用最后一次提交的作业"


@pytest.mark.asyncio
async def test_distinct_keys_run_independently():
    runner = _KeyedSingleFlight()
    started: list[str] = []
    gate = asyncio.Event()

    def job(tag: str):
        async def run() -> None:
            started.append(tag)
            await gate.wait()
        return run

    assert runner.submit(("k", "a"), job("a"))
    assert runner.submit(("k", "b"), job("b"))
    await asyncio.sleep(0.01)
    assert sorted(started) == ["a", "b"]
    gate.set()
    assert await runner.drain(timeout=5)


@pytest.mark.asyncio
async def test_a_failing_job_does_not_swallow_the_pending_rerun():
    runner = _KeyedSingleFlight()
    release = asyncio.Event()
    ran: list[str] = []

    async def boom() -> None:
        ran.append("boom")
        await release.wait()
        raise RuntimeError("parse exploded")

    async def after() -> None:
        ran.append("after")

    runner.submit(("k", "s"), boom)
    await asyncio.sleep(0)
    runner.submit(("k", "s"), after)
    release.set()
    assert await runner.drain(timeout=5)
    assert ran == ["boom", "after"]
    # 跑完即从登记表里消失，下一次提交重新起任务
    assert runner.submit(("k", "s"), after) is True
    assert await runner.drain(timeout=5)


@pytest.mark.asyncio
async def test_tasks_are_strongly_referenced_until_done():
    """事件循环只弱引用任务：没人持有的在飞任务可能被 GC 回收、作业半途消失。"""
    runner = _KeyedSingleFlight()
    finished = asyncio.Event()

    async def slow() -> None:
        await asyncio.sleep(0.05)
        finished.set()

    runner.submit(("k", "s"), slow)
    gc.collect()
    assert runner.in_flight == 1
    await asyncio.wait_for(finished.wait(), 2)
    await asyncio.sleep(0)
    assert runner.in_flight == 0


@pytest.mark.asyncio
async def test_drain_is_bounded_and_cancels_leftovers():
    runner = _KeyedSingleFlight()
    cancelled = asyncio.Event()

    async def stuck() -> None:
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    runner.submit(("k", "s"), stuck)
    loop = asyncio.get_running_loop()
    started = loop.time()
    assert await runner.drain(timeout=0.2) is False
    assert loop.time() - started < 1.0
    assert cancelled.is_set()
    assert runner.in_flight == 0


@pytest.mark.asyncio
async def test_cancelled_jobs_are_named_with_how_to_recover(caplog):
    """被取消的作业要能事后定位该回填谁：WARNING 逐个列出 kind:id，并按类别写明补救方式。

    SessionEnd 终测与子 agent 记账之后没有下一次事件，"下一次捕获会重算"对它们不成立。
    """
    runner = _KeyedSingleFlight()

    async def stuck() -> None:
        await asyncio.sleep(60)

    async def quick() -> None:
        return None

    runner.submit(("leader-usage", "sess-end-1"), stuck)
    runner.submit(("subagent-usage", "agent-42"), stuck)
    runner.submit(("workflow-reconcile", "/proj/x"), stuck)
    runner.submit(("leader-usage", "sess-done"), quick)
    with caplog.at_level(logging.WARNING, logger="aiteam.api.hook_translator"):
        assert await runner.drain(timeout=0.2) is False

    [message] = [r.getMessage() for r in caplog.records if "background drain" in r.getMessage()]
    for key in ("leader-usage:sess-end-1", "subagent-usage:agent-42", "workflow-reconcile:/proj/x"):
        assert key in message
    assert "sess-done" not in message, "跑完的作业不算被取消"
    assert "subagent-usage not recomputed" in message
    assert "scripts/backfill_token_usage.py" in message


def test_every_scheduled_job_kind_has_a_recovery_note():
    """新增后台作业类别时必须同时写明它被取消后怎么补，否则 drain 的 WARNING 无从指引。"""
    source = inspect.getsource(hook_translator.HookTranslator)
    kinds = set(re.findall(r'_schedule\(\s*\(\s*"([a-z-]+)"', source))
    assert kinds == {"leader-usage", "subagent-usage", "workflow-reconcile"}
    assert kinds <= set(hook_translator._CANCELLED_JOB_RECOVERY)  # noqa: SLF001


def test_drain_bound_leaves_room_inside_the_restart_budget():
    """os_restart_api 给旧进程 10s 退出，_delayed_exit 在里面依次走完：0.5s 让出、drain、
    WAL checkpoint（sqlite3 连接超时 5s）、诊断 flush（0.25s）。drain 上限放大到挤爆这
    10s，旧进程就会被判 shutdown_timeout、新版本拉不起来。"""
    assert 0 < BACKGROUND_DRAIN_TIMEOUT_SECONDS
    assert 0.5 + BACKGROUND_DRAIN_TIMEOUT_SECONDS + 5 + 0.25 < 10


# ============================================================
# 关停：先有界 drain，再关库
# ============================================================


@pytest.mark.asyncio
async def test_cleanup_drains_background_work_before_closing_the_database(monkeypatch):
    calls: list[tuple[str, object]] = []

    class _Translator:
        async def drain(self, timeout=None):
            calls.append(("drain", timeout))
            return True

    async def fake_close_db() -> None:
        calls.append(("close_db", None))

    monkeypatch.setattr(deps, "_hook_translator", _Translator())
    monkeypatch.setattr(deps, "_watchdog_runner", None)
    monkeypatch.setattr(deps, "_reaper", None)
    monkeypatch.setattr(deps, "close_db", fake_close_db)
    await deps.cleanup_dependencies()
    assert calls == [("drain", BACKGROUND_DRAIN_TIMEOUT_SECONDS), ("close_db", None)]


@pytest.mark.asyncio
@pytest.mark.parametrize("drain_raises", [False, True], ids=["drains", "drain-raises"])
async def test_http_shutdown_drains_before_checkpoint_and_exit(monkeypatch, drain_raises):
    """os_restart_api 走的 HTTP shutdown 硬退、不经 lifespan：drain 得在它自己的退出序列里，
    排在 WAL checkpoint 与 os._exit 之前；drain 抛错也绝不能拦住退出。整栈落库版本见
    test_http_shutdown_drain.py。"""
    calls: list[tuple[str, object]] = []

    class _Translator:
        async def drain(self, timeout=None):
            calls.append(("drain", timeout))
            if drain_raises:
                raise RuntimeError("drain exploded")
            return True

    monkeypatch.setenv("AITEAM_DIAGNOSTICS_ENABLED", "0")
    monkeypatch.setattr(deps, "_hook_translator", _Translator())
    monkeypatch.setattr(deps, "_repository", None)
    monkeypatch.setattr(system, "_wal_checkpoint_best_effort", lambda: calls.append(("checkpoint", None)))
    monkeypatch.setattr(system.os, "_exit", lambda code: calls.append(("exit", code)))
    await system._delayed_exit()  # noqa: SLF001
    assert calls == [("drain", BACKGROUND_DRAIN_TIMEOUT_SECONDS), ("checkpoint", None), ("exit", 0)]


# ============================================================
# 节流账记在调度时
# ============================================================


def _transcript(tmp_path: Path) -> Path:
    path = tmp_path / "s.jsonl"
    path.write_text(
        '{"type":"assistant","requestId":"r1","message":{"model":"claude-opus-5",'
        '"usage":{"input_tokens":1,"output_tokens":2}}}\n',
        encoding="utf-8",
    )
    return path


def test_claim_books_the_throttle_before_the_parse_runs(tmp_path: Path):
    """解析在飞期间再来的 Stop 必须判"节流"，而不是再排一轮 —— 记账在调度时。"""
    meter = leader_usage.SessionUsageMeter(min_interval_seconds=300, mtime_advance_seconds=600)
    t = _transcript(tmp_path)
    now = utc_now()
    assert meter.claim("s1", t, now=now) is None
    # measure 还没跑：第二个 Stop 已经被挡下
    assert meter.claim("s1", t, now=now + timedelta(seconds=5)) == leader_usage.SKIP_THROTTLED
    assert meter.claim("s1", t, force=True, now=now + timedelta(seconds=5)) is None
    snapshot, reason = meter.measure("s1", t)
    assert reason is None
    assert (snapshot.input_tokens, snapshot.output_tokens) == (1, 2)


def test_claim_on_a_vanished_file_is_named(tmp_path: Path):
    meter = leader_usage.SessionUsageMeter()
    assert meter.claim("s1", tmp_path / "gone.jsonl", force=True) == leader_usage.SKIP_UNREADABLE


def test_measure_reports_unreadable_when_the_file_disappears_after_the_claim(tmp_path: Path):
    meter = leader_usage.SessionUsageMeter()
    t = _transcript(tmp_path)
    assert meter.claim("s1", t, force=True) is None
    t.unlink()
    assert meter.measure("s1", t) == (None, leader_usage.SKIP_UNREADABLE)


def test_cursor_table_is_an_lru_capped_at_64_sessions(tmp_path: Path):
    meter = leader_usage.SessionUsageMeter()
    t = _transcript(tmp_path)
    for i in range(70):
        meter.measure(f"s{i}", t)
    cursors = meter._cursors  # noqa: SLF001
    assert len(cursors) == leader_usage._MAX_CURSORS == 64  # noqa: SLF001
    assert "s5" not in cursors and "s6" in cursors  # 最早的 6 个被淘汰
    meter.measure("s6", t)  # 最近用过 -> 挪到队尾
    meter.measure("s70", t)  # 再来一个新会话：淘汰的是最久没用的 s7，不是 s6
    assert "s6" in cursors and "s7" not in cursors
    assert len(cursors) == 64


def test_module_exposes_the_deferred_marker():
    """回执里的 deferred 与强制定格告警的豁免用的是同一个常量。"""
    assert leader_usage.DEFERRED == "deferred"
    assert hook_translator.leader_usage.DEFERRED is leader_usage.DEFERRED


# ============================================================
# 作业等本次事件处理结束才起跑
# ============================================================


class _NullBus:
    async def emit(self, *_args, **_kwargs) -> None:
        return None


@pytest.mark.asyncio
async def test_jobs_scheduled_by_a_handler_start_only_after_it_returns():
    """作业的写库不许与同一请求后半段的写库交错（单连接库上会直接撞 SQL statements in progress）。"""
    translator = hook_translator.HookTranslator(repo=None, event_bus=_NullBus())  # type: ignore[arg-type]
    started: list[str] = []

    async def job() -> None:
        started.append("job")

    async def handler(_payload: dict) -> dict:
        translator._schedule(("k", "s"), job)  # noqa: SLF001
        await asyncio.sleep(0.02)  # 处理后半段还在 await
        assert started == [], "作业在处理结束前就起跑了"
        return {"status": "ok"}

    translator._on_teammate_idle = handler  # type: ignore[method-assign]  # noqa: SLF001
    assert await translator.handle_event({"hook_event_name": "TeammateIdle"}) == {"status": "ok"}
    assert await translator.drain(timeout=5)
    assert started == ["job"]


@pytest.mark.asyncio
async def test_jobs_still_start_when_the_handler_raises():
    """调度即记了节流账：处理后半段抛错也不能让已调度的测量凭空消失。"""
    translator = hook_translator.HookTranslator(repo=None, event_bus=_NullBus())  # type: ignore[arg-type]
    started: list[str] = []

    async def job() -> None:
        started.append("job")

    async def handler(_payload: dict) -> dict:
        translator._schedule(("k", "s"), job)  # noqa: SLF001
        raise RuntimeError("cleanup failed after scheduling")

    translator._on_teammate_idle = handler  # type: ignore[method-assign]  # noqa: SLF001
    with pytest.raises(RuntimeError):
        await translator.handle_event({"hook_event_name": "TeammateIdle"})
    assert await translator.drain(timeout=5)
    assert started == ["job"]
