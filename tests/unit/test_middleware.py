"""Unit tests for HTTP input protection and SQLite admission control.

Regression coverage for AI-company issue #1: bodies larger than the old
16 KB window used to bypass guardrail checks entirely.
"""

from __future__ import annotations

import asyncio
import json
import re

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel
from starlette.requests import Request
from starlette.responses import JSONResponse

from aiteam.api.guardrails import _DANGEROUS_RULES, check_dict
from aiteam.api.hook_translator import GUARDRAIL_FLAGS_FIELD
from aiteam.api.middleware import (
    _MAX_BODY_BYTES,
    InputGuardrailMiddleware,
    SQLiteConcurrencyMiddleware,
    _rule_id,
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


# 触发文本拼接构造：本文件经工具读写时，原文不会让自己的 hook 事件被入口扫描吞掉。
# 规则显示名也不写原文（删表规则的显示名本身就是触发文本），按 ID 从规则表反查。
_TRAVERSAL = "cat " + "../" * 2 + "etc/hosts"
_SCRIPT = "<" + "script src=x>"
_LABEL_BY_ID = {_rule_id(label): label for _, label in _DANGEROUS_RULES}
_HOOK_TRIGGERS = [
    pytest.param({"command": _TRAVERSAL}, "path_traversal", id="traversal"),
    pytest.param({"content": "ex" + "ec(code)"}, "code_injection_exec", id="exec"),
    pytest.param({"content": _SCRIPT}, "xss_script_tag", id="script"),
    pytest.param({"command": "rm" + " -rf /tmp/x"}, "destructive_shell_command", id="rm"),
    pytest.param({"stdout": "DROP" + " TABLE t;"}, "sql_drop_table", id="drop"),
    pytest.param({"content": "__imp" + "ort__('os')"}, "python_code_injection_import", id="import"),
    pytest.param({"content": "ev" + "al(expr)"}, "code_injection_eval", id="eval"),
]


class _Item(BaseModel):
    """A body model: FastAPI parses it for any JSON-ish content type, so the guardrail must too."""

    command: str = ""


def _build_hook_client() -> TestClient:
    app = FastAPI()
    app.add_middleware(InputGuardrailMiddleware)

    async def seen(request: Request) -> dict:
        return {"flags": getattr(request.state, "guardrail_flags", None)}

    for path in ("/api/hooks/event", "/api/hooks/eventx", "/api/hooks/event/",
                 "/api/hooks/diagnose_denial", "/api/echo"):
        app.add_api_route(path, seen, methods=["GET", "POST", "PUT", "PATCH"])

    @app.post("/api/model")
    async def model(item: _Item) -> dict:
        return {"command": item.command}

    return TestClient(app, raise_server_exceptions=True)


hook_client = _build_hook_client()


class TestHookEventFlagOnly:
    """POST /api/hooks/event: scanned and logged, a match flags instead of blocking (d3e0d6bb)."""

    @pytest.mark.parametrize(("tool_input", "rule"), _HOOK_TRIGGERS)
    def test_match_is_flagged_not_blocked(self, tool_input, rule):
        resp = hook_client.post("/api/hooks/event", json={"tool_input": tool_input})
        assert resp.status_code == 200
        assert resp.json() == {"flags": [rule]}

    @pytest.mark.parametrize(("tool_input", "rule"), _HOOK_TRIGGERS)
    def test_same_body_elsewhere_is_still_blocked(self, tool_input, rule):
        resp = hook_client.post("/api/echo", json={"tool_input": tool_input})
        assert resp.status_code == 400
        assert any(v.endswith(_LABEL_BY_ID[rule]) for v in resp.json()["violations"])

    def test_clean_body_sets_no_flags(self):
        resp = hook_client.post("/api/hooks/event", json={"tool_input": {"command": "git status"}})
        assert resp.json() == {"flags": None}

    def test_flags_are_rule_ids_deduplicated_in_order(self):
        body = {"tool_input": {"command": _TRAVERSAL, "content": _SCRIPT},
                "tool_response": {"stdout": _TRAVERSAL}}
        resp = hook_client.post("/api/hooks/event", json=body)
        assert resp.json() == {"flags": ["path_traversal", "xss_script_tag"]}

    @pytest.mark.parametrize(("tool_input", "rule"), _HOOK_TRIGGERS)
    def test_recorded_flags_can_be_quoted_to_a_guarded_route(self, tool_input, rule):
        """标记会随事件 data 被引用、转发到其它受 guardrail 保护的入口：它自己不能再触发规则。"""
        flags = hook_client.post("/api/hooks/event", json={"tool_input": tool_input}).json()["flags"]
        resp = hook_client.post("/api/echo", json={"event": {GUARDRAIL_FLAGS_FIELD: flags}})
        assert resp.status_code == 200, resp.text

    def test_flag_is_logged_in_the_violation_format(self, caplog):
        with caplog.at_level("WARNING", logger="aiteam.api.middleware"):
            hook_client.post("/api/hooks/event", json={"tool_input": {"command": _TRAVERSAL}})
        [line] = [r.getMessage() for r in caplog.records if "Guardrail L1" in r.getMessage()]
        assert line == (
            "Guardrail L1 flagged, not blocked: request POST /api/hooks/event"
            " - violations: ['tool_input.command: path traversal']"
        )

    @pytest.mark.parametrize("path", ["/api/hooks/eventx", "/api/hooks/event/", "/api/hooks/diagnose_denial"])
    def test_lookalike_paths_are_not_exempt(self, path):
        resp = hook_client.post(path, json={"tool_input": {"command": _TRAVERSAL}})
        assert resp.status_code == 400

    @pytest.mark.parametrize("method", ["PUT", "PATCH"])
    def test_other_body_methods_are_not_exempt(self, method):
        resp = hook_client.request(method, "/api/hooks/event", json={"tool_input": {"command": _TRAVERSAL}})
        assert resp.status_code == 400

    def test_get_is_not_exempt_either(self):
        # GET 从来不扫（不属于 _BODY_METHODS）；这里钉住它也不会拿到标记，行为与之前一致。
        resp = hook_client.request("GET", "/api/hooks/event", json={"tool_input": {"command": _TRAVERSAL}})
        assert resp.json() == {"flags": None}

    def test_oversized_hook_body_still_rejected_413(self):
        resp = hook_client.post("/api/hooks/event", json={"pad": "x" * (_MAX_BODY_BYTES + 1024)})
        assert resp.status_code == 413


class TestJsonContentTypeParsing:
    """The guardrail scans every body FastAPI would read as JSON; header spelling is no bypass."""

    @pytest.mark.parametrize("content_type", [
        "application/json",
        "Application/JSON",
        "APPLICATION/JSON; charset=utf-8",
        " application/json ",
        "application/vnd.api+json",
        "application/merge-patch+json",
        "Application/Problem+JSON",
    ])
    def test_json_variants_are_scanned(self, content_type):
        resp = hook_client.post(
            "/api/model", content=json.dumps({"command": _TRAVERSAL}).encode(),
            headers={"content-type": content_type},
        )
        assert resp.status_code == 400

    def test_missing_content_type_is_scanned(self):
        resp = hook_client.post("/api/model", content=json.dumps({"command": _TRAVERSAL}).encode())
        assert resp.status_code == 400

    @pytest.mark.parametrize("content_type", [
        "application/jsonp",
        "application/json-seq",
        "text/plain; note=application/json",
    ])
    def test_legacy_substring_rule_is_kept_as_floor(self, content_type):
        resp = hook_client.post(
            "/api/echo", content=json.dumps({"command": _TRAVERSAL}).encode(),
            headers={"content-type": content_type},
        )
        assert resp.status_code == 400

    @pytest.mark.parametrize("content_type", ["text/plain", "text/json", "application/x-json", "multipart/form-data"])
    def test_non_json_types_are_not_scanned(self, content_type):
        resp = hook_client.post(
            "/api/echo", content=json.dumps({"command": _TRAVERSAL}).encode(),
            headers={"content-type": content_type},
        )
        assert resp.status_code == 200

    def test_variant_on_hook_route_is_scanned_and_flagged(self):
        resp = hook_client.post(
            "/api/hooks/event", content=json.dumps({"command": _TRAVERSAL}).encode(),
            headers={"content-type": "Application/JSON"},
        )
        assert resp.json() == {"flags": ["path_traversal"]}


class TestGuardrailRuleIds:
    """Flags are stored as rule IDs; every rule's ID must be stable, distinct and inert (machine check)."""

    def test_every_rule_id_is_snake_case_distinct_and_trips_no_rule(self):
        ids = [_rule_id(label) for _, label in _DANGEROUS_RULES]
        assert len(set(ids)) == len(ids), ids
        assert all(re.fullmatch(r"[a-z0-9]+(?:_[a-z0-9]+)*", rule_id) for rule_id in ids), ids
        result = check_dict({GUARDRAIL_FLAGS_FIELD: ids})
        assert result["safe"], result["violations"]

    def test_hook_triggers_cover_every_rule(self):
        # 新增规则时 _HOOK_TRIGGERS 跟着补一例，上面的标记 / 转发 / 400 三组用例才覆盖得到它。
        assert {param.values[1] for param in _HOOK_TRIGGERS} == set(_LABEL_BY_ID)


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
