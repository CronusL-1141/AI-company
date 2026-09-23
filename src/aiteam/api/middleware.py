"""AI Team OS — HTTP middleware stack.

Contains:
- SQLiteConcurrencyMiddleware: throttle concurrent DB requests.
- InputGuardrailMiddleware: L1 basic input validation (dangerous patterns).
"""

from __future__ import annotations

import asyncio
import email.message
import json
import logging
import math
import re
import time

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from aiteam.api.guardrails import check_dict

logger = logging.getLogger(__name__)

# Paths that don't hit the database (skip throttling)
_SKIP_PATHS = frozenset({"/api/health", "/docs", "/openapi.json", "/favicon.ico"})

# Methods that carry a JSON body worth inspecting
_BODY_METHODS = frozenset({"POST", "PUT", "PATCH"})

# Paths to skip guardrail checks (static assets, docs)
_GUARDRAIL_SKIP_PREFIXES = ("/assets", "/docs", "/openapi", "/favicon")

# Hard cap on JSON body size for guardrail-checked routes (2 MB).
# Oversized bodies are REJECTED with 413, never waved through — a pass-through
# here lets attackers pad payloads past the check (AI-company issue #1).
# Legitimate large payloads (reports, meeting minutes) stay well under 2 MB.
_MAX_BODY_BYTES = 2 * 1024 * 1024

# 命中规则只标记、不拦的入口（用户 2026-09-23 裁定，任务墙 d3e0d6bb）：hook 事件接收端。
# 载荷来自本机 hook 进程，工具输入/输出里正常出现的上跳路径、执行调用等字样一命中就整条
# 400，Pre/Post 事件随之丢失、span 卡在 running。这里照扫、照记日志，把规则 ID 放进
# request.state.guardrail_flags 由路由记进事件，然后放行；2MB 上限照旧 413。
# 方法与路径都精确匹配：GET/PUT/PATCH、/api/hooks/eventx、/api/hooks/diagnose_denial 不在内。
_FLAG_ONLY_ROUTES = frozenset({("POST", "/api/hooks/event")})


def _is_json_body(content_type: str) -> bool:
    """Whether a route may read this body as JSON, so the guardrail must scan it.

    判据对齐 FastAPI 自己的解析（routing.get_request_handler）：media type 不分大小写，
    application/json 与 application/*+json 都按 JSON 读。旧判据是区分大小写的子串匹配，
    换个大小写或用 +json 后缀，任何路由都能带着触发文本绕过扫描。不带 content-type 也扫：
    strict_content_type 之前的 FastAPI（pyproject 允许 >=0.115）会把这种 body 当 JSON 读。
    旧子串判据保留为下限，只加严不放宽；解析不了的一律当 JSON 扫（不是 JSON 会在 loads 放行）。
    """
    if not content_type or "application/json" in content_type.lower():
        return True
    try:
        message = email.message.Message()
        message["content-type"] = content_type
        if message.get_content_maintype() != "application":
            return False
        subtype = message.get_content_subtype()
    except Exception:  # noqa: BLE001 - unparseable header: scan rather than skip
        return True
    return subtype == "json" or subtype.endswith("+json")


def _rule_id(label: str) -> str:
    """Stable snake_case ID for a guardrail rule label, e.g. "path traversal" -> "path_traversal".

    落库的标记用 ID 不用显示名：删表规则的显示名本身就命中删表正则，事件 data 一旦被引用
    或转发到其它受 guardrail 保护的入口（memo/report/channel），标记字段自己就会把整条请求
    拦成 400。ID 只含 [a-z0-9_]，现有规则一条都匹配不上（test_middleware 对全部规则机检）。
    """
    return re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")


def _rule_ids(violations: list[str]) -> list[str]:
    """Rule IDs from check_dict entries ("<field path>: <rule label>"), de-duplicated in order."""
    ids: list[str] = []
    for entry in violations:
        rule_id = _rule_id(entry.rsplit(": ", 1)[-1])
        if rule_id not in ids:
            ids.append(rule_id)
    return ids


class InputGuardrailMiddleware(BaseHTTPMiddleware):
    """L1 input validation — reject requests containing dangerous patterns.

    Only inspects POST/PUT/PATCH JSON bodies on /api/* paths.
    PII detections are logged but never block the request.
    Routes in ``_FLAG_ONLY_ROUTES`` are scanned and logged the same way, but a
    match only sets ``request.state.guardrail_flags`` instead of returning 400.
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        # Only check mutation requests to API paths
        if request.method not in _BODY_METHODS:
            return await call_next(request)
        path = request.url.path
        if not path.startswith("/api/") or any(path.startswith(p) for p in _GUARDRAIL_SKIP_PREFIXES):
            return await call_next(request)

        if not _is_json_body(request.headers.get("content-type", "")):
            return await call_next(request)

        try:
            raw = await request.body()
        except Exception:
            # Read error — let the route handler deal with it
            return await call_next(request)

        if len(raw) > _MAX_BODY_BYTES:
            logger.warning(
                "Guardrail L1 rejected oversized body (%d bytes): %s %s",
                len(raw), request.method, path,
            )
            return JSONResponse(
                {
                    "detail": "请求体过大，已被安全策略拒绝",
                    "max_bytes": _MAX_BODY_BYTES,
                    "_hint": "请求体超过 2MB 上限，请拆分或缩减内容",
                },
                status_code=413,
            )

        try:
            payload = json.loads(raw)
        except Exception:
            # Malformed JSON — let the route handler deal with it
            return await call_next(request)

        result = check_dict(payload)
        if not result["safe"]:
            violations = result["violations"]
            if (request.method, path) in _FLAG_ONLY_ROUTES:
                # 与拦截那条同前缀、同 violations 格式，grep "Guardrail L1" 两种都能捞到。
                logger.warning(
                    "Guardrail L1 flagged, not blocked: request %s %s - violations: %s",
                    request.method, path, violations,
                )
                request.state.guardrail_flags = _rule_ids(violations)
                return await call_next(request)
            logger.warning(
                "Guardrail L1 blocked request %s %s — violations: %s",
                request.method, path, violations,
            )
            return JSONResponse(
                {
                    "detail": "请求被安全策略拒绝",
                    "violations": violations,
                    "_hint": "输入包含危险模式，请检查请求内容",
                },
                status_code=400,
            )

        return await call_next(request)


class SQLiteConcurrencyMiddleware(BaseHTTPMiddleware):
    """Limit concurrent requests that access SQLite.

    Reserve admission capacity for hook events without increasing DB concurrency.

    Ordinary traffic acquires its lane before the total semaphore, so queued
    page requests cannot occupy the reserved capacity. With defaults, normal
    peak concurrency drops from five to four, even when hooks are idle.

    MCP is a transport shell: its tools call the separately throttled REST API.
    Counting both layers can exhaust all normal permits before the inner call
    runs, particularly when the transport waits for a complete JSON response.
    """

    def __init__(
        self, app, max_concurrent: int = 5, queue_timeout: float = 30.0,
        reserved: int = 1,
    ):
        super().__init__(app)
        if (
            max_concurrent < 1 or reserved < 0 or reserved > max_concurrent
            or (max_concurrent > 1 and reserved == max_concurrent)
        ):
            raise ValueError("Require positive capacity and leave at least one normal slot")
        if not math.isfinite(queue_timeout) or queue_timeout <= 0:
            raise ValueError("queue_timeout must be finite and positive")
        self._semaphore = asyncio.Semaphore(max_concurrent)
        # Keep normal traffic live for the single-slot compatibility case.
        self._normal_semaphore = asyncio.Semaphore(max(1, max_concurrent - reserved))
        self._queue_timeout = queue_timeout
        self._active = 0
        self._total = 0

    async def dispatch(self, request: Request, call_next) -> Response:
        path = request.url.path
        # MCP control requests and streams must remain available even when DB
        # capacity is full. Match the mount boundary, not lookalikes like /mcpx.
        if path == "/mcp" or path.startswith("/mcp/"):
            return await call_next(request)

        # Skip other non-DB paths.
        if path in _SKIP_PATHS or path.startswith("/assets"):
            return await call_next(request)

        queued = time.monotonic()
        normal_acquired = False
        total_acquired = False
        start = None
        is_hook = request.method == "POST" and request.url.path == "/api/hooks/event"
        try:
            try:
                # One deadline covers both queues, not one timeout per semaphore.
                async with asyncio.timeout(self._queue_timeout):
                    if not is_hook:
                        await self._normal_semaphore.acquire()
                        normal_acquired = True
                    await self._semaphore.acquire()
                    total_acquired = True
            except TimeoutError:
                logger.warning(
                    "Request queue timeout (%ss): %s %s",
                    self._queue_timeout, request.method, request.url.path,
                )
                return JSONResponse(
                    {"detail": "Server busy, please retry"},
                    status_code=503,
                    headers={"Server-Timing": f"queue;dur={(time.monotonic() - queued) * 1000:.3f}"},
                )

            self._active += 1
            self._total += 1
            start = time.monotonic()
            response = await call_next(request)
            timing = (
                f"queue;dur={(start - queued) * 1000:.3f}, "
                f"handler;dur={(time.monotonic() - start) * 1000:.3f}"
            )
            existing = response.headers.get("Server-Timing")
            response.headers["Server-Timing"] = f"{existing}, {timing}" if existing else timing
            return response
        finally:
            if total_acquired:
                self._semaphore.release()
            if normal_acquired:
                self._normal_semaphore.release()
            if start is not None:
                elapsed = time.monotonic() - start
                self._active -= 1
                if elapsed > 5.0:
                    logger.warning(
                        "Slow request (%.1fs): %s %s",
                        elapsed, request.method, request.url.path,
                    )
