"""Unit tests for InputGuardrailMiddleware — L1 guardrail HTTP layer.

Regression coverage for AI-company issue #1: bodies larger than the old
16 KB window used to bypass guardrail checks entirely.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from aiteam.api.middleware import _MAX_BODY_BYTES, InputGuardrailMiddleware


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
