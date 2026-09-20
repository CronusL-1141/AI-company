"""Unit tests for HTTP input protection and SQLite admission control.

Regression coverage for AI-company issue #1: bodies larger than the old
16 KB window used to bypass guardrail checks entirely.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request
from starlette.responses import JSONResponse

from aiteam.api.middleware import (
    _MAX_BODY_BYTES,
    InputGuardrailMiddleware,
    SQLiteConcurrencyMiddleware,
)


def _build_client() -> TestClient:
    app = FastAPI()
    app.add_middleware(InputGuardrailMiddleware)

    @app.post("/api/echo")
    async def echo() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/api/echo")
    async def echo_get() -> dict[str, bool]:
        return {"ok": True}

    @app.post("/internal/echo")
    async def echo_internal() -> dict[str, bool]:
        return {"ok": True}

    return TestClient(app)


client = _build_client()


class TestSmallBodies:
    def test_clean_payload_passes(self):
        resp = client.post("/api/echo", json={"title": "实现登录功能"})
        assert resp.status_code == 200

    def test_malicious_payload_blocked(self):
        resp = client.post("/api/echo", json={"cmd": "rm -rf /"})
        assert resp.status_code == 400
        assert resp.json()["violations"]

    def test_malformed_json_passes_through(self):
        resp = client.post(
            "/api/echo", content=b"not json{{{",
            headers={"content-type": "application/json"},
        )
        # Route handler's concern, not the guardrail's
        assert resp.status_code == 200


class TestLargeBodies:
    """Regression: >16KB used to bypass checks entirely (issue #1)."""

    def test_padded_malicious_payload_blocked(self):
        payload = {"pad": "x" * 20_000, "cmd": "rm -rf /"}
        resp = client.post("/api/echo", json=payload)
        assert resp.status_code == 400
        assert resp.json()["violations"]

    def test_deeply_padded_malicious_payload_blocked(self):
        payload = {"report": "章节内容 " * 30_000, "extra": "__import__('os')"}
        resp = client.post("/api/echo", json=payload)
        assert resp.status_code == 400

    def test_large_clean_payload_passes(self):
        # Legitimate large content (report_save etc.) must NOT be rejected —
        # this is why a blanket 413 on >16KB was not an option.
        payload = {"content": "会议纪要与审计报告内容。" * 10_000}
        resp = client.post("/api/echo", json=payload)
        assert resp.status_code == 200

    def test_oversized_body_rejected_413(self):
        payload = {"pad": "x" * (_MAX_BODY_BYTES + 1024)}
        resp = client.post("/api/echo", json=payload)
        assert resp.status_code == 413
        assert resp.json()["max_bytes"] == _MAX_BODY_BYTES


class TestScopeExclusions:
    def test_get_requests_not_checked(self):
        resp = client.get("/api/echo")
        assert resp.status_code == 200

    def test_non_api_paths_not_checked(self):
        resp = client.post("/internal/echo", json={"cmd": "rm -rf /"})
        assert resp.status_code == 200

    def test_non_json_content_type_not_checked(self):
        resp = client.post(
            "/api/echo", content=b"cmd=rm+-rf+/",
            headers={"content-type": "application/x-www-form-urlencoded"},
        )
        assert resp.status_code == 200


def _request(path: str, method: str = "POST") -> Request:
    return Request({"type": "http", "method": method, "path": path, "headers": []})


@pytest.mark.parametrize("method", ["GET", "POST", "DELETE", "HEAD", "OPTIONS"])
@pytest.mark.parametrize("path", ["/mcp", "/mcp/", "/mcp/session"])
async def test_mcp_transport_remains_available_when_db_capacity_is_full(method, path):
    middleware = SQLiteConcurrencyMiddleware(FastAPI(), max_concurrent=1, queue_timeout=0.02)
    await middleware._semaphore.acquire()
    await middleware._normal_semaphore.acquire()

    async def transport(request):
        return JSONResponse({"method": request.method})

    try:
        response = await middleware.dispatch(_request(path, method), transport)
        assert response.status_code == 200
        assert "server-timing" not in response.headers
        assert middleware._active == middleware._total == 0
    finally:
        middleware._semaphore.release()
        middleware._normal_semaphore.release()


@pytest.mark.parametrize("path", ["/mcp-other", "/mcproxy", "/MCP/", "/api/mcp/", "/api/projects"])
async def test_mcp_exemption_does_not_bypass_db_admission_for_other_paths(path):
    middleware = SQLiteConcurrencyMiddleware(FastAPI(), max_concurrent=1, queue_timeout=0.02)
    await middleware._normal_semaphore.acquire()

    async def handler(request):
        pytest.fail("Non-MCP request bypassed the database queue")

    try:
        response = await middleware.dispatch(_request(path), handler)
        assert response.status_code == 503
    finally:
        middleware._normal_semaphore.release()
    assert middleware._semaphore._value == middleware._normal_semaphore._value == 1


async def test_48_nested_mcp_requests_keep_db_caps_and_hook_reservation():
    middleware = SQLiteConcurrencyMiddleware(FastAPI(), queue_timeout=1.0)
    four_shells = asyncio.Event()
    start_rest = asyncio.Event()
    four_rest = asyncio.Event()
    five_total = asyncio.Event()
    release_rest = asyncio.Event()
    shell_count = normal_active = active = normal_peak = peak = 0

    async def database(request):
        nonlocal normal_active, active, normal_peak, peak
        normal = request.url.path != "/api/hooks/event"
        normal_active += int(normal)
        active += 1
        normal_peak = max(normal_peak, normal_active)
        peak = max(peak, active)
        if normal_active == 4:
            four_rest.set()
        if active == 5:
            five_total.set()
        try:
            await release_rest.wait()
            return JSONResponse({"ok": True})
        finally:
            normal_active -= int(normal)
            active -= 1

    async def shell(request):
        nonlocal shell_count
        shell_count += 1
        if shell_count >= 4:
            four_shells.set()
        await start_rest.wait()
        return await middleware.dispatch(_request("/api/projects"), database)

    tasks = [asyncio.create_task(middleware.dispatch(_request("/mcp/"), shell)) for _ in range(48)]
    try:
        await asyncio.wait_for(four_shells.wait(), 5)
        start_rest.set()
        await asyncio.wait_for(four_rest.wait(), 5)
        tasks.append(asyncio.create_task(middleware.dispatch(_request("/api/hooks/event"), database)))
        await asyncio.wait_for(five_total.wait(), 5)
        assert active == 5 and normal_active == 4
        release_rest.set()
        responses = await asyncio.gather(*tasks)
        assert [response.status_code for response in responses] == [200] * 49
        assert normal_peak == 4 and peak == 5
        assert middleware._total == 49  # Nested REST only, never the MCP shell.
    finally:
        start_rest.set()
        release_rest.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    assert middleware._active == 0
    assert middleware._semaphore._value == 5
    assert middleware._normal_semaphore._value == 4
