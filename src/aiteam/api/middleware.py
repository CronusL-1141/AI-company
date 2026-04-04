"""AI Team OS — SQLite concurrency middleware.

Limits concurrent database-accessing requests to prevent SQLite lock contention.
SQLite allows only one writer at a time; without throttling, concurrent agent
requests cause deadlocks and API hangs.
"""

import asyncio
import logging
import time

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

logger = logging.getLogger(__name__)

# Paths that don't hit the database (skip throttling)
_SKIP_PATHS = frozenset({"/api/health", "/docs", "/openapi.json", "/favicon.ico"})


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
