"""ASGI diagnostics preserve responses, streams and exception semantics."""

from __future__ import annotations

import json
import threading
import time

import httpx
import pytest

from aiteam import diagnostics
from aiteam.api.request_diagnostics import RequestDiagnosticsMiddleware


@pytest.fixture
def diagnostic_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("AITEAM_DIAGNOSTICS_DIR", str(tmp_path))
    monkeypatch.setenv("AITEAM_DIAGNOSTICS_ENABLED", "1")
    return tmp_path


async def streaming_app(scope, receive, send):
    await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"text/plain")]})
    await send({"type": "http.response.body", "body": b"one", "more_body": True})
    await send({"type": "http.response.body", "body": b"two", "more_body": False})


async def test_request_id_joins_server_receipt_and_response_without_query_values(diagnostic_dir):
    request_id = "a" * 32
    transport = httpx.ASGITransport(app=RequestDiagnosticsMiddleware(streaming_app))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/test?secret=query-canary", headers={
            "X-Aiteam-Request-Id": request_id, "Authorization": "Bearer auth-canary",
        })
    assert response.text == "onetwo"
    assert response.headers["X-Aiteam-Request-Id"] == request_id
    assert diagnostics.flush_diagnostics(timeout=2)
    text = "".join(file.read_text() for file in diagnostic_dir.glob("*.jsonl"))
    items = [json.loads(line) for line in text.splitlines()]
    assert [item["event"] for item in items] == ["http.server.received", "http.server.response"]
    assert all(item["request_id"] == request_id for item in items)
    assert "canary" not in text


@pytest.mark.parametrize("request_id", [None, "untrusted-header-canary", "b" * 33])
async def test_uncorrelated_or_invalid_requests_are_unchanged(diagnostic_dir, request_id):
    transport = httpx.ASGITransport(app=RequestDiagnosticsMiddleware(streaming_app))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/", headers={"X-Aiteam-Request-Id": request_id} if request_id else {})
    assert response.text == "onetwo"
    assert "X-Aiteam-Request-Id" not in response.headers
    assert list(diagnostic_dir.iterdir()) == []


async def test_exceptions_are_recorded_without_changing_propagation(diagnostic_dir):
    async def broken_app(scope, receive, send):
        raise RuntimeError("exception-text-canary")

    transport = httpx.ASGITransport(app=RequestDiagnosticsMiddleware(broken_app))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        with pytest.raises(RuntimeError, match="exception-text-canary"):
            await client.get("/api/test", headers={"X-Aiteam-Request-Id": "a" * 32})
    assert diagnostics.flush_diagnostics(timeout=2)
    text = "".join(file.read_text() for file in diagnostic_dir.glob("*.jsonl"))
    items = [json.loads(line) for line in text.splitlines()]
    assert items[-1]["event"] == "http.server.failed"
    assert items[-1]["error_type"] == "RuntimeError"
    assert "exception-text-canary" not in text


async def test_slow_disk_does_not_block_request_event_loop(diagnostic_dir, monkeypatch):
    diagnostics.record_event("test.initialize_sink")
    assert diagnostics.flush_diagnostics(timeout=2)
    entered = threading.Event()
    release = threading.Event()
    original = diagnostics._handler.write

    def slow_write(line):
        entered.set()
        assert release.wait(timeout=5)
        original(line)

    monkeypatch.setattr(diagnostics._handler, "write", slow_write)
    diagnostics.record_event("test.slow_disk")
    assert entered.wait(timeout=1)
    transport = httpx.ASGITransport(app=RequestDiagnosticsMiddleware(streaming_app))
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            started = time.monotonic()
            response = await client.get("/api/test", headers={"X-Aiteam-Request-Id": "a" * 32})
            assert response.status_code == 200
            assert time.monotonic() - started < 1
            assert not release.is_set()
    finally:
        release.set()
        assert diagnostics.flush_diagnostics(timeout=2)
