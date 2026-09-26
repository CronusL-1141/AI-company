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
from starlette.requests import ClientDisconnect, Request
from starlette.responses import JSONResponse, Response

from aiteam.api.guardrails import check_dict
from aiteam.api.request_ledger import request_ledger
from aiteam.clock import utc_now
from aiteam.diagnostics import record_event

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

# Hook ingest: slow-request threshold on queue + handler time. Clients give up at
# 1.5s, so the generic 5s handler-only threshold never saw a lost receipt.
_HOOK_SLOW_SECONDS = 1.0
# Slow hook requests logged one by one per minute; past this, the rest of the
# minute is folded into a single summary line. Under load hundreds of hook
# requests a minute cross the threshold, and one line each would rotate the
# debug log's history away (it happened to the 09-25 diagnosis). Every slow
# request is still counted in the request ledger ("slow").
_HOOK_SLOW_LOGGED_PER_MINUTE = 5

# Hook ingest outcomes that no access log shows (the client is gone, so uvicorn
# drops the response line):
#   client_gone - client left while the event queued for a DB slot. The body was
#       already read, so the event is still handled; only the receipt is lost. A
#       lower bound: a close uvicorn has not processed yet is not seen.
#   body_lost - client left before the body was read. uvicorn discards a buffered
#       body once the peer closes, so the event is lost. The one remaining
#       server-side loss path; only a client-side replay queue recovers it.
# Redelivered hook events (``_hook_replay`` marker), counted by the hook route:
#   replayed - every redelivery received.
#   replay_duplicate - of those, the ones whose first delivery had landed after all
#       (answered from the receipt). Their share is how often "timed out" in the
#       client's ledger actually meant "landed, receipt lost".
#   replay_skipped - redelivered lifecycle events, which have no replay semantics
#       yet (hook_translator.REPLAYABLE_EVENTS) and are not handled.
#   slow - queue + handler over _HOOK_SLOW_SECONDS.
# These are this process's totals since start. Each count also goes to the request
# ledger, which rolls it up hourly into the event stream (and on exit), so the
# numbers outlive a restart; os_health_check reads them via /api/hooks/ingest-stats.
hook_ingest_stats = {
    "client_gone": 0, "body_lost": 0, "slow": 0,
    "replayed": 0, "replay_duplicate": 0, "replay_skipped": 0,
}


def note_hook_ingest(name: str) -> int:
    """Count one hook ingest outcome in this process and in the request ledger; returns the total."""
    hook_ingest_stats[name] += 1
    request_ledger.note_hook_ingest(name)
    return hook_ingest_stats[name]


class _SlowHookLog:
    """Per-minute folding of slow hook request log lines; no timer, rolled by requests."""

    def __init__(self) -> None:
        self._minute: str | None = None
        self._logged = 0
        self._folded = 0
        self._worst = (0.0, 0.0, 0.0)  # total, queue, handler of the slowest folded one

    def observe(self, total: float, queued: float, handled: float, path: str) -> None:
        """Called for every finished hook request; logs or folds the slow ones."""
        minute = utc_now().strftime("%Y-%m-%dT%H:%MZ")
        if minute != self._minute:
            self._emit_summary()
            self._minute, self._logged, self._folded, self._worst = minute, 0, 0, (0.0, 0.0, 0.0)
        if total <= _HOOK_SLOW_SECONDS:
            return
        note_hook_ingest("slow")
        if self._logged < _HOOK_SLOW_LOGGED_PER_MINUTE:
            self._logged += 1
            logger.warning(
                "Slow hook request (%.2fs: queue %.2fs, handler %.2fs): POST %s",
                total, queued, handled, path,
            )
            return
        self._folded += 1
        if total > self._worst[0]:
            self._worst = (total, queued, handled)

    def _emit_summary(self) -> None:
        if self._folded:
            total, queued, handled = self._worst
            logger.warning(
                "Slow hook requests in %s: %d more not logged one by one "
                "(slowest %.2fs: queue %.2fs, handler %.2fs)",
                self._minute, self._folded, total, queued, handled,
            )


_slow_hook_log = _SlowHookLog()


def _is_hook_ingest(request: Request) -> bool:
    return request.method == "POST" and request.url.path == "/api/hooks/event"


def _oversized_body_response(request: Request, size: int | None) -> JSONResponse:
    logger.warning(
        "Guardrail L1 rejected oversized body (%s bytes): %s %s",
        size if size is not None else "?", request.method, request.url.path,
    )
    return JSONResponse(
        {
            "detail": "请求体过大，已被安全策略拒绝",
            "max_bytes": _MAX_BODY_BYTES,
            "_hint": "请求体超过 2MB 上限，请拆分或缩减内容",
        },
        status_code=413,
    )


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
            return _oversized_body_response(request, len(raw))

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

    Hook events read their body before queueing (see ``dispatch``), so an event
    whose client gave up while it waited is still handled; only its receipt is lost.
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

        is_hook = _is_hook_ingest(request)
        body_read = False
        if is_hook:
            # Read the body before queueing. uvicorn answers receive() on a closed
            # connection with http.disconnect even when the body is already
            # buffered, so an event whose client gave up while it queued used to
            # reach the routes body-less and die as a silent 400. Read here, the
            # body is replayed downstream by BaseHTTPMiddleware and the event is
            # handled whether or not anyone is still waiting for the receipt.
            early, body_read = await self._preread_body(request)
            if early is not None:
                return early

        queued = time.monotonic()
        normal_acquired = False
        total_acquired = False
        start = None
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
            if body_read and await request.is_disconnected():
                self._note_client_gone(request, start - queued)
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
                finished = time.monotonic()
                elapsed = finished - start
                self._active -= 1
                if is_hook:
                    _slow_hook_log.observe(
                        finished - queued, start - queued, elapsed, request.url.path,
                    )
                elif elapsed > 5.0:
                    logger.warning(
                        "Slow request (%.1fs): %s %s",
                        elapsed, request.method, request.url.path,
                    )

    @staticmethod
    async def _preread_body(request: Request) -> tuple[Response | None, bool]:
        """Buffer a hook body up front. Returns (early response, whether the body was read).

        Requires a Content-Length (every hook client sends one) so the read is
        bounded before it starts; without one the body stays for the routes to
        read, as before. The size cap is the guardrail's, answered the same way.
        """
        declared = request.headers.get("content-length", "")
        if not declared.isdigit():
            return None, False
        size = int(declared)
        if size > _MAX_BODY_BYTES:
            return _oversized_body_response(request, size), False
        try:
            await request.body()
        except ClientDisconnect:
            SQLiteConcurrencyMiddleware._note_body_lost(request)
            return JSONResponse({"detail": "client disconnected"}, status_code=400), False
        return None, True

    @staticmethod
    def _note_body_lost(request: Request) -> None:
        total = note_hook_ingest("body_lost")
        logger.warning(
            "hook_body_lost: client gone before the body was read (uvicorn discards a buffered "
            "body once the peer closes); event lost (total %d): %s %s",
            total, request.method, request.url.path,
        )
        record_event(
            "http.server.hook_body_lost", method=request.method, path=request.url.path,
            body_lost_total=total,
        )

    @staticmethod
    def _note_client_gone(request: Request, queued_seconds: float) -> None:
        total = note_hook_ingest("client_gone")
        logger.warning(
            "hook_client_gone: client left after %.2fs in queue; handling the event anyway "
            "(receipt lost, total %d): %s %s",
            queued_seconds, total, request.method, request.url.path,
        )
        record_event(
            "http.server.hook_client_gone", method=request.method, path=request.url.path,
            queue_ms=round(queued_seconds * 1000, 3), client_gone_total=total,
        )
