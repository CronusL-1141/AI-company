"""AI Team OS — HTTP middleware stack.

Contains:
- SQLiteConcurrencyMiddleware: throttle concurrent DB requests.
- InputGuardrailMiddleware: L1 basic input validation (dangerous patterns).
"""

from __future__ import annotations

import asyncio
import json
import logging
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

# Maximum body size to parse for guardrail checks (16 KB)
_MAX_BODY_BYTES = 16_384


class InputGuardrailMiddleware(BaseHTTPMiddleware):
    """L1 input validation — reject requests containing dangerous patterns.

    Only inspects POST/PUT/PATCH JSON bodies on /api/* paths.
    PII detections are logged but never block the request.
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        # Only check mutation requests to API paths
        if request.method not in _BODY_METHODS:
            return await call_next(request)
        path = request.url.path
        if not path.startswith("/api/") or any(path.startswith(p) for p in _GUARDRAIL_SKIP_PREFIXES):
            return await call_next(request)

        content_type = request.headers.get("content-type", "")
        if "application/json" not in content_type:
            return await call_next(request)

        # Read body (limited size to avoid memory abuse)
        try:
            raw = await request.body()
            if len(raw) > _MAX_BODY_BYTES:
                # Body too large for inline check — pass through, don't block
                return await call_next(request)
            payload = json.loads(raw)
        except Exception:
            # Malformed JSON or read error — let the route handler deal with it
            return await call_next(request)

        result = check_dict(payload)
        if not result["safe"]:
            violations = result["violations"]
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

    Uses an asyncio.Semaphore to queue excess requests instead of
    letting them all compete for SQLite locks simultaneously.
    """

    def __init__(self, app, max_concurrent: int = 5, queue_timeout: float = 30.0):
        super().__init__(app)
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._queue_timeout = queue_timeout
        self._active = 0
        self._total = 0

    async def dispatch(self, request: Request, call_next) -> Response:
        # Skip non-DB paths
        if request.url.path in _SKIP_PATHS or request.url.path.startswith("/assets"):
            return await call_next(request)

        try:
            await asyncio.wait_for(
                self._semaphore.acquire(), timeout=self._queue_timeout
            )
        except TimeoutError:
            logger.warning(
                "Request queue timeout (%ss): %s %s",
                self._queue_timeout, request.method, request.url.path,
            )
            return JSONResponse(
                {"detail": "Server busy, please retry"},
                status_code=503,
            )

        self._active += 1
        self._total += 1
        start = time.monotonic()
        try:
            response = await call_next(request)
            return response
        finally:
            elapsed = time.monotonic() - start
            self._active -= 1
            self._semaphore.release()
            if elapsed > 5.0:
                logger.warning(
                    "Slow request (%.1fs): %s %s",
                    elapsed, request.method, request.url.path,
                )
