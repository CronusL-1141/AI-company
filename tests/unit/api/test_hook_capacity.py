"""Hook admission capacity under real ASGI and temporary SQLite traffic."""

from __future__ import annotations

import asyncio
import json
import time

import aiosqlite
import httpx
import pytest
from fastapi import FastAPI, Request
from starlette.responses import JSONResponse

from aiteam.api import deps
from aiteam.api import event_bus as event_bus_module
from aiteam.api.event_bus import EventBus
from aiteam.api.hook_translator import HookTranslator
from aiteam.api.middleware import SQLiteConcurrencyMiddleware
from aiteam.api.routes import hooks
from aiteam.api.ws.manager import ConnectionManager
from aiteam.storage.connection import get_engine
from aiteam.storage.repository import StorageRepository


@pytest.mark.asyncio
@pytest.mark.parametrize("reserved", [0, 1], ids=["shared-baseline", "reserved-fix"])
@pytest.mark.parametrize("payload", [
    {
        "hook_event_name": "PreToolUse",
        "session_id": "synthetic-cc-capacity",
        "tool_name": "Bash",
        "tool_input": {"command": "echo capacity-probe"},
        "tool_use_id": "synthetic-cc-tool-use",
    },
    {
        "hook_event_name": "PreToolUse",
        "session_id": "synthetic-codex-capacity",
        "tool_name": "Bash",
        "tool_input": {"command": "echo capacity-probe"},
        "call_id": "synthetic-codex-call",
        "turn_id": "synthetic-codex-turn",
    },
], ids=["synthetic-cc", "synthetic-codex"])
async def test_hook_ack_during_slow_page_queries(tmp_path, monkeypatch, payload, reserved):
    """Synthetic ingress bodies, not host captures; production schema through DB."""
    app = FastAPI()
    app.add_middleware(SQLiteConcurrencyMiddleware, reserved=reserved)
    app.include_router(hooks.router)
    database = tmp_path / "capacity.sqlite"
    database_url = f"sqlite+aiosqlite:///{database}"
    repo = StorageRepository(db_url=database_url)
    await repo.init_db()
    bus = EventBus(repo=repo)
    translator = HookTranslator(repo=repo, event_bus=bus)
    app.dependency_overrides.update({
        deps.get_repository: lambda: repo,
        deps.get_event_bus: lambda: bus,
        deps.get_hook_translator: lambda: translator,
    })
    monkeypatch.setattr(event_bus_module, "ws_manager", ConnectionManager())
    monkeypatch.setattr(event_bus_module.cfg, "SLACK_WEBHOOK_URL", "")
    monkeypatch.delenv(hooks.HOOK_RAW_DUMP_ENV, raising=False)
    entered = 0
    pages_ready = asyncio.Event()

    @app.get("/api/projects")
    async def page():
        nonlocal entered
        async with aiosqlite.connect(database) as db:
            await db.create_function("slow_page", 0, lambda: time.sleep(1.7) or 1)
            entered += 1
            if entered >= 4:
                pages_ready.set()
            async with db.execute("SELECT slow_page(), COUNT(*) FROM events") as cursor:
                return {"rows": await cursor.fetchall()}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        pages = [asyncio.create_task(client.get("/api/projects")) for _ in range(5)]
        try:
            await asyncio.wait_for(pages_ready.wait(), 2)
            started = time.monotonic()
            response = await client.post("/api/hooks/event", json=payload)
            elapsed = time.monotonic() - started
            print(f"{payload['session_id']}, reserved={reserved}: acknowledgement {elapsed:.3f}s")
            assert response.status_code == 200
            assert response.json() == {"decision": "allow"}
            assert "queue;dur=" in response.headers["server-timing"]
            assert "handler;dur=" in response.headers["server-timing"]
            async with aiosqlite.connect(database) as db:
                async with db.execute("SELECT type, source, data FROM events") as cursor:
                    rows = await cursor.fetchall()
            assert len(rows) == 1
            event_type, source, data = rows[0]
            assert event_type == "cc.tool_use"
            assert source == f"session:{payload['session_id']}"
            stored = json.loads(data)
            assert stored["tool_name"] == payload["tool_name"]
            assert stored["session_id"] == payload["session_id"]
            assert "capacity-probe" in stored["tool_input_summary"]
            if reserved:
                assert elapsed < 1.5
            else:
                assert elapsed >= 1.5
        finally:
            await asyncio.gather(*pages)
            app.dependency_overrides.clear()
            await get_engine(database_url).dispose()


def _request(path="/api/projects", method="GET"):
    return Request({"type": "http", "method": method, "path": path, "headers": []})


async def _ok(request):
    return JSONResponse({"ok": True}, headers={"Server-Timing": "db;dur=0.5"})


@pytest.mark.asyncio
async def test_normal_recovery_and_total_concurrency_cap():
    middleware = SQLiteConcurrencyMiddleware(FastAPI())
    release = asyncio.Event()
    four_normals = asyncio.Event()
    five_total = asyncio.Event()
    active = normal_active = peak = normal_peak = 0

    async def handler(request):
        nonlocal active, normal_active, peak, normal_peak
        normal = request.method == "GET"
        active += 1
        normal_active += int(normal)
        peak = max(peak, active)
        normal_peak = max(normal_peak, normal_active)
        if normal_active == 4:
            four_normals.set()
        if active == 5:
            five_total.set()
        try:
            await release.wait()
            return await _ok(request)
        finally:
            active -= 1
            normal_active -= int(normal)

    tasks = [asyncio.create_task(middleware.dispatch(_request(), handler)) for _ in range(12)]
    try:
        await asyncio.wait_for(four_normals.wait(), 1)
        tasks.extend(
            asyncio.create_task(middleware.dispatch(_request("/api/hooks/event", "POST"), handler))
            for _ in range(6)
        )
        await asyncio.wait_for(five_total.wait(), 1)
        assert active == 5
    finally:
        release.set()
        responses = await asyncio.gather(*tasks)
    assert peak == 5
    assert normal_peak == 4
    assert all(response.status_code == 200 for response in responses)
    assert middleware._active == 0
    assert middleware._semaphore._value == 5
    assert middleware._normal_semaphore._value == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("method,path,is_hook", [
    ("POST", "/api/hooks/event", True),
    ("GET", "/api/hooks/event", False),
    ("PUT", "/api/hooks/event", False),
    ("POST", "/api/hooks/event/", False),
    ("POST", "/api/hooks/events", False),
    ("POST", "/api/hooks/other", False),
])
async def test_only_exact_event_post_gets_reserved_capacity(method, path, is_hook):
    middleware = SQLiteConcurrencyMiddleware(FastAPI(), max_concurrent=2, queue_timeout=0.03)
    await middleware._normal_semaphore.acquire()
    try:
        response = await middleware.dispatch(_request(path, method), _ok)
        assert response.status_code == (200 if is_hook else 503)
        if not is_hook:
            assert response.body == b'{"detail":"Server busy, please retry"}'
            assert "handler" not in response.headers["server-timing"]
        else:
            assert response.headers["server-timing"].startswith("db;dur=0.5, queue;dur=")
    finally:
        middleware._normal_semaphore.release()
    assert middleware._semaphore._value == 2
    assert middleware._normal_semaphore._value == 1


@pytest.mark.asyncio
async def test_both_queues_share_one_deadline():
    middleware = SQLiteConcurrencyMiddleware(FastAPI(), max_concurrent=2, queue_timeout=0.20)
    await middleware._normal_semaphore.acquire()
    await middleware._semaphore.acquire()
    await middleware._semaphore.acquire()
    started = time.monotonic()
    task = asyncio.create_task(middleware.dispatch(_request(), _ok))
    try:
        await asyncio.sleep(0.12)
        middleware._normal_semaphore.release()
        response = await task
        elapsed = time.monotonic() - started
        assert response.status_code == 503
        assert 0.18 <= elapsed < 0.29
        assert middleware._normal_semaphore._value == 1
    finally:
        middleware._semaphore.release()
        middleware._semaphore.release()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    assert (await middleware.dispatch(_request(), _ok)).status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["normal_queue", "total_queue", "handler"])
async def test_cancellation_releases_every_acquired_permit(stage):
    middleware = SQLiteConcurrencyMiddleware(FastAPI(), max_concurrent=2)
    entered = asyncio.Event()
    blocker = asyncio.Event()

    async def handler(request):
        entered.set()
        await blocker.wait()
        return await _ok(request)

    if stage == "normal_queue":
        await middleware._normal_semaphore.acquire()
    elif stage == "total_queue":
        await middleware._semaphore.acquire()
        await middleware._semaphore.acquire()
    task = asyncio.create_task(middleware.dispatch(_request(), handler))
    try:
        if stage == "handler":
            await asyncio.wait_for(entered.wait(), 1)
        else:
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        if stage == "normal_queue":
            middleware._normal_semaphore.release()
        elif stage == "total_queue":
            middleware._semaphore.release()
            middleware._semaphore.release()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    assert middleware._active == 0
    assert middleware._normal_semaphore._value == 1
    assert middleware._semaphore._value == 2
    assert (await middleware.dispatch(_request(), _ok)).status_code == 200


@pytest.mark.asyncio
async def test_handler_exception_releases_permits_without_becoming_queue_timeout():
    middleware = SQLiteConcurrencyMiddleware(FastAPI())

    async def handler(request):
        raise TimeoutError("handler failure")

    with pytest.raises(TimeoutError, match="handler failure"):
        await middleware.dispatch(_request(), handler)
    assert middleware._semaphore._value == 5
    assert middleware._normal_semaphore._value == 4
    assert middleware._active == 0
    assert (await middleware.dispatch(_request(), _ok)).status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize("reserved", [0, 1])
async def test_single_slot_remains_live_and_hooks_obey_total_gate(reserved):
    middleware = SQLiteConcurrencyMiddleware(
        FastAPI(), max_concurrent=1, reserved=reserved, queue_timeout=0.02,
    )
    assert (await middleware.dispatch(_request(), _ok)).status_code == 200
    await middleware._semaphore.acquire()
    try:
        response = await middleware.dispatch(_request("/api/hooks/event", "POST"), _ok)
        assert response.status_code == 503
    finally:
        middleware._semaphore.release()
    assert (await middleware.dispatch(_request("/api/hooks/event", "POST"), _ok)).status_code == 200
    assert middleware._semaphore._value == 1


@pytest.mark.parametrize("kwargs", [
    {"max_concurrent": 0}, {"reserved": -1}, {"reserved": 6}, {"reserved": 5},
    {"queue_timeout": 0}, {"queue_timeout": float("nan")},
    {"queue_timeout": float("inf")}, {"queue_timeout": float("-inf")},
])
def test_invalid_configuration_fails_fast(kwargs):
    with pytest.raises(ValueError):
        SQLiteConcurrencyMiddleware(FastAPI(), **kwargs)


@pytest.mark.asyncio
@pytest.mark.parametrize("reserved", [0, 2, 4])
async def test_configurable_reservation_limits_normal_lane(reserved):
    middleware = SQLiteConcurrencyMiddleware(FastAPI(), reserved=reserved, queue_timeout=0.02)
    assert middleware._normal_semaphore._value == 5 - reserved
    for _ in range(5 - reserved):
        await middleware._normal_semaphore.acquire()
    try:
        assert (await middleware.dispatch(_request(), _ok)).status_code == 503
    finally:
        for _ in range(5 - reserved):
            middleware._normal_semaphore.release()
    assert middleware._semaphore._value == 5
    assert (await middleware.dispatch(_request(), _ok)).status_code == 200
