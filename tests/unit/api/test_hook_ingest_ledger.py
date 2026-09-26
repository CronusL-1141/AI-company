"""Hook ingest counters in the request ledger, the read-side summary, and slow-log folding."""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

import pytest
from fastapi import FastAPI
from starlette.requests import Request
from starlette.responses import JSONResponse

from aiteam.api import middleware as middleware_module
from aiteam.api import request_ledger as ledger_module
from aiteam.api.request_ledger import RequestLedger, summarize_hook_ingest
from aiteam.clock import utc_now


class _Bus:
    def __init__(self):
        self.rollups: list[dict] = []

    async def emit(self, event_type, source, data, **_kw):
        if event_type == ledger_module.ROLLUP_EVENT:
            self.rollups.append(data)


@pytest.mark.asyncio()
async def test_ingest_counts_ride_the_hourly_rollup_of_their_own_hour():
    bus = _Bus()
    ledger = RequestLedger(bus)
    ledger.record("POST", "/api/hooks/event", "hook-or-mcp", bucket="2026-09-26T10")
    ledger.note_hook_ingest("client_gone", bucket="2026-09-26T10")
    ledger.note_hook_ingest("client_gone", bucket="2026-09-26T10")
    # A body lost in the next hour, before any request of that hour was recorded.
    ledger.note_hook_ingest("body_lost", bucket="2026-09-26T11")
    await ledger.observe_bucket("2026-09-26T11")
    assert [r["bucket"] for r in bus.rollups] == ["2026-09-26T10"]
    assert bus.rollups[0]["hook_ingest"] == {"client_gone": 2}
    assert ledger.pending_hook_ingest()["body_lost"] == 1

    # An ingest-only hour is rolled up too, not orphaned.
    await ledger.observe_bucket("2026-09-26T13")
    assert bus.rollups[-1]["bucket"] == "2026-09-26T11"
    assert bus.rollups[-1]["hook_ingest"] == {"body_lost": 1}
    assert bus.rollups[-1]["counts"] == {}
    assert ledger.ingest_since_start["client_gone"] == 2


@pytest.mark.asyncio()
async def test_exit_flush_writes_the_open_bucket_and_a_final_marker():
    bus = _Bus()
    ledger = RequestLedger(bus)
    await ledger.flush_all()
    assert bus.rollups[-1]["final"] is True  # even with nothing counted

    ledger.record("POST", "/api/hooks/event", "hook-or-mcp", bucket="2026-09-26T10")
    ledger.note_hook_ingest("client_gone", bucket="2026-09-26T10")
    await ledger.flush_all()
    last = bus.rollups[-1]
    assert last["final"] is True and last["hook_ingest"] == {"client_gone": 1}
    assert last["pid"] == ledger.pid
    assert ledger.pending_hook_ingest()["client_gone"] == 0


@pytest.mark.asyncio()
async def test_summary_sums_the_window_adds_memory_and_flags_unflushed_processes():
    bus = _Bus()
    ledger = RequestLedger(bus)
    ledger.note_hook_ingest("client_gone")
    now = utc_now()
    since = now - timedelta(hours=24)
    old = (now - timedelta(hours=30)).strftime("%Y-%m-%dT%H")
    recent = (now - timedelta(hours=2)).strftime("%Y-%m-%dT%H")
    rollups = [
        (now, {"bucket": old, "hook_ingest": {"client_gone": 50}, "pid": 1, "process_started_at": "a",
               "final": True}),
        (now, {"bucket": recent, "hook_ingest": {"client_gone": 3, "body_lost": 1}, "pid": 2,
               "process_started_at": "b"}),
        (now, {"bucket": recent, "hook_ingest": {"client_gone": 4}, "pid": 2, "process_started_at": "b",
               "final": True}),
        (now, {"bucket": recent, "counts": {}, "pid": 3, "process_started_at": "c", "opened": True}),
    ]
    summary = summarize_hook_ingest(rollups, ledger, since)
    assert summary["window"]["client_gone"] == 3 + 4 + 1  # old hour outside, memory inside
    assert summary["window"]["body_lost"] == 1
    assert summary["processes_without_final_rollup"] == 1  # pid 3 opened, never closed
    assert summary["complete"] is False
    assert "lower bound" in summary["notes"]["client_gone"]


def _hook_request() -> Request:
    return Request({"type": "http", "method": "POST", "path": "/api/hooks/event", "headers": []})


@pytest.mark.asyncio()
async def test_slow_hook_logs_fold_past_the_per_minute_quota(monkeypatch, caplog):
    minute = ["2026-09-26T10:00"]

    class _Clock:
        def strftime(self, _fmt):
            return minute[0] + "Z"

    monkeypatch.setattr(middleware_module, "utc_now", lambda: _Clock())
    monkeypatch.setattr(middleware_module, "_slow_hook_log", middleware_module._SlowHookLog())
    monkeypatch.setattr(middleware_module, "_HOOK_SLOW_SECONDS", 0.05)
    slow_before = middleware_module.hook_ingest_stats["slow"]
    concurrency = middleware_module.SQLiteConcurrencyMiddleware(FastAPI())

    async def slow(_request):
        await asyncio.sleep(0.08)
        return JSONResponse({"ok": True})

    async def fast(_request):
        return JSONResponse({"ok": True})

    with caplog.at_level(logging.WARNING, logger="aiteam.api.middleware"):
        await asyncio.gather(*(concurrency.dispatch(_hook_request(), slow) for _ in range(8)))
        minute[0] = "2026-09-26T10:01"
        await concurrency.dispatch(_hook_request(), fast)

    lines = [r.getMessage() for r in caplog.records]
    assert sum(line.startswith("Slow hook request (") for line in lines) == 5
    (summary,) = [line for line in lines if line.startswith("Slow hook requests in")]
    assert "2026-09-26T10:00Z: 3 more" in summary
    assert middleware_module.hook_ingest_stats["slow"] - slow_before == 8  # every one is counted


def test_a_process_whose_last_row_rolled_the_hour_before_the_window_is_still_seen():
    """Started 09:05, its 09 hour rolled at 10:05 (row bucket T09), killed at 10:50.

    A one-hour window from 10:00 has no row with a bucket in it, but it lost the
    10:05-10:50 counts: the window must not read as complete.
    """
    ledger = RequestLedger(_Bus())
    since = utc_now().replace(minute=0, second=0, microsecond=0)
    before = since - timedelta(hours=1)
    bucket_before = before.strftime("%Y-%m-%dT%H")
    rollups = [
        (before + timedelta(minutes=5), {"bucket": bucket_before, "counts": {}, "pid": 7,
                                         "process_started_at": "p", "opened": True}),
        (since + timedelta(minutes=5), {"bucket": bucket_before, "counts": {"GET /x|y": 3},
                                        "pid": 7, "process_started_at": "p"}),
    ]
    summary = summarize_hook_ingest(rollups, ledger, since)
    assert summary["processes_without_final_rollup"] == 1
    assert summary["complete"] is False


@pytest.mark.asyncio()
async def test_first_rollover_after_startup_writes_a_row_even_when_idle():
    """A process idle since startup must show up again in the hour its traffic resumes."""
    bus = _Bus()
    ledger = RequestLedger(bus)
    await ledger.mark_opened()
    opened_bucket = bus.rollups[0]["bucket"]
    later = "9999-12-31T23"  # any later hour
    await ledger.observe_bucket(later)
    assert [r["bucket"] for r in bus.rollups] == [opened_bucket, opened_bucket]
    assert bus.rollups[1]["counts"] == {} and "final" not in bus.rollups[1]
    await ledger.observe_bucket("9999-12-31T24")
    assert len(bus.rollups) == 2  # later empty hours stay silent


@pytest.mark.asyncio()
async def test_first_request_of_a_new_hour_counts_in_its_own_hour(monkeypatch):
    from starlette.applications import Starlette
    from starlette.responses import PlainTextResponse
    from starlette.routing import Route
    from starlette.testclient import TestClient

    bus = _Bus()
    ledger = RequestLedger(bus)
    hour = ["2026-09-26T10"]
    monkeypatch.setattr(ledger_module, "hour_bucket", lambda now=None: hour[0])
    app = Starlette(routes=[Route("/api/x", lambda _r: PlainTextResponse("ok"))])
    app.add_middleware(ledger_module.RequestLedgerMiddleware, ledger=ledger)
    with TestClient(app) as client:
        client.get("/api/x")
        hour[0] = "2026-09-26T11"
        client.get("/api/x")
    (rollup,) = bus.rollups
    assert rollup["bucket"] == "2026-09-26T10"
    assert rollup["total"] == 1  # the 11 o'clock request is not in the 10 o'clock row
