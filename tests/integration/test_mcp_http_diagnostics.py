"""Exercise diagnostics through the real urllib transport on loopback only."""

from __future__ import annotations

import json
import socket
import threading
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from aiteam.diagnostics import flush_diagnostics
from aiteam.mcp import _base


@pytest.fixture
def http_fixture(monkeypatch, tmp_path):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            failed = urllib.parse.urlsplit(self.path).path.startswith("/fail")
            self.send_response(502 if failed else 200)
            self.send_header("Via", "1.1 loopback-test")
            self.send_header("Content-Type", "application/json")
            self.send_header("Set-Cookie", "cookie-secret-canary")
            self.end_headers()
            self.wfile.write(json.dumps({
                "success": not failed,
                "request_id": self.headers.get("X-Aiteam-Request-Id"),
                "detail": "body-secret-canary",
            }).encode())

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("AITEAM_API_URL", f"http://127.0.0.1:{server.server_port}")
    monkeypatch.setenv("AITEAM_DIAGNOSTICS_DIR", str(tmp_path))
    monkeypatch.setenv("AITEAM_DIAGNOSTICS_ENABLED", "1")
    monkeypatch.setattr(urllib.request, "_opener", urllib.request.build_opener(urllib.request.ProxyHandler({})))
    try:
        yield tmp_path, server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
        assert not thread.is_alive()


def _records(directory):
    assert flush_diagnostics(timeout=2)
    return [json.loads(line) for file in directory.glob("*.jsonl") for line in file.read_text().splitlines()]


def test_real_502_has_correlated_transport_evidence_without_secrets(http_fixture):
    directory, server = http_fixture
    result = _base._api_call("GET", "/fail?token=query-secret-canary", extra_headers={
        "Authorization": "Bearer header-secret-canary",
    })
    assert result["success"] is False
    assert result["error"] == "HTTP 502: Bad Gateway"
    records = _records(directory)
    failures = [r for r in records if r["event"] == "http.client.failed"]
    assert len(failures) == 1, "The real HTTP failure must persist a diagnostic event"
    failure = failures[0]
    assert failure["status"] == 502
    assert failure["response_headers"]["via"] == "1.1 loopback-test"
    assert failure["elapsed_ms"] >= 0
    assert failure["peer"] == {"host": "127.0.0.1", "port": server.server_port}
    assert len(failure["request_id"]) == 32
    assert any(r["event"] == "http.client.started" and r["request_id"] == failure["request_id"] for r in records)
    text = json.dumps(records)
    for secret in ("query-secret-canary", "header-secret-canary", "cookie-secret-canary", "body-secret-canary"):
        assert secret not in text


def test_success_has_request_id_and_response_metadata(http_fixture):
    directory, _server = http_fixture
    result = _base._api_call("GET", "/ok")
    assert result["success"] is True
    assert len(result["request_id"]) == 32
    done = [r for r in _records(directory) if r["event"] == "http.client.completed"]
    assert len(done) == 1
    assert done[0]["request_id"] == result["request_id"]
    assert done[0]["status"] == 200


def test_refused_connection_records_errno_and_destination(http_fixture, monkeypatch):
    directory, _server = http_fixture
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        monkeypatch.setenv("AITEAM_API_URL", f"http://127.0.0.1:{port}")
        result = _base._api_call("GET", "/api/health")
    assert result["success"] is False
    failure = next(r for r in _records(directory) if r["event"] == "http.client.failed")
    assert failure["error_type"] == "URLError"
    assert isinstance(failure["errno"], int)
    assert failure["url"] == f"http://127.0.0.1:{port}/api/health"


def test_unwritable_sink_does_not_change_http_result(http_fixture, monkeypatch):
    directory, _server = http_fixture
    not_a_directory = directory / "blocked"
    not_a_directory.write_text("occupied")
    monkeypatch.setenv("AITEAM_DIAGNOSTICS_DIR", str(not_a_directory))
    assert _base._api_call("GET", "/ok")["success"] is True
    assert _base._api_call("GET", "/fail")["error"] == "HTTP 502: Bad Gateway"


async def test_real_mcp_health_tool_records_the_underlying_502(http_fixture, monkeypatch):
    from fastmcp import Client

    from aiteam.mcp.server import mcp

    directory, server = http_fixture
    monkeypatch.setenv("AITEAM_API_URL", f"http://127.0.0.1:{server.server_port}/fail")
    async with Client(mcp) as client:
        reply = await client.call_tool("os_health_check", {})
    assert reply.data["status"] == "unhealthy"
    assert reply.data["error"] == "HTTP 502: Bad Gateway"
    failure = next(r for r in _records(directory) if r["event"] == "http.client.failed")
    assert failure["status"] == 502
    assert failure["url"].endswith("/fail/api/teams")
    assert failure["peer"]["port"] == server.server_port


def test_proxy_502_records_proxy_peer_separately_from_api_destination(http_fixture, monkeypatch):
    directory, proxy = http_fixture
    proxy_url = f"http://user:proxy-password-canary@127.0.0.1:{proxy.server_port}"
    monkeypatch.setenv("AITEAM_API_URL", "http://os-fixture.invalid:54321")
    monkeypatch.setattr(urllib.request, "proxy_bypass", lambda host: False)
    monkeypatch.setattr(urllib.request, "getproxies", lambda: {"http": proxy_url})
    monkeypatch.setattr(urllib.request, "_opener", urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": proxy_url}),
    ))
    result = _base._api_call("GET", "/fail?token=query-secret-canary")
    assert result["error"] == "HTTP 502: Bad Gateway"
    records = _records(directory)
    started = next(r for r in records if r["event"] == "http.client.started")
    failed = next(r for r in records if r["event"] == "http.client.failed")
    assert started["proxy"]["target_bypass"] is False
    assert started["proxy"]["proxies"]["http"]["port"] == proxy.server_port
    assert failed["url"] == "http://os-fixture.invalid:54321/fail"
    assert failed["peer"] == {"host": "127.0.0.1", "port": proxy.server_port}
    assert "proxy-password-canary" not in json.dumps(records)
    assert "query-secret-canary" not in json.dumps(records)
