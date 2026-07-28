"""HTTP 请求级账本 — 给"零调用"判断补上第二个口径。

此前判断"某工具没人用"只有 CC 侧的 MCP 工具调用观测这**一个**采集面。但 MCP
工具内部一律转成 HTTP 打到本地 API，Dashboard、hook、脚本、别的会话也都走同一
批端点——于是"零调用"实际只等于"这一个面没看见"，既排除不了采集断链，也排除
不了口径隔离。补上请求级账本后，零调用才有第二个口径可交叉验证。

三条设计约束（都不是偷懒，是本仓既有红线）：

* **不新建表**。I10 机检 ORM 声明的表集合与实库一致，而生产实例不能为了建表重启；
  events 本就是 append-only 账本，聚合行天然属于它。
* **不造新噪声**。逐请求落库会把账本淹掉（本机 events 已 5 万+），故在内存里按
  「方法 × 路径模板 × 来源」计数，**按小时**聚合成一条 rollup 事件。选小时是因为
  零调用判断的时间分辨率本就是天/周级，小时已经远超需要；再细只涨行数不涨信息。
* **不引定时器**。本仓刻意没有后台守护/cron，故用**惰性翻滚**：下一个请求进来时
  发现跨桶了，才把上一桶落库。代价是最后一桶要等下次有请求才落地——对"谁在被调
  用"这个问题无影响（没有请求本身就是答案）。

路径一律用**模板**（/api/tasks/{task_id}）而非实际路径，否则 id 会把基数打爆。
"""

from __future__ import annotations

import logging
from datetime import datetime

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

logger = logging.getLogger(__name__)

ROLLUP_EVENT = "api.request_rollup"

# Paths that say nothing about who uses which capability.
_SKIP_PREFIXES = ("/assets", "/docs", "/openapi", "/favicon", "/ws")


def classify_source(headers, user_agent: str) -> str:
    """Coarse caller bucket — deliberately 4 values, not a fingerprint.

    The point is only to tell "nobody calls this" apart from "one surface calls
    it a lot", so anything finer would be noise.
    """
    explicit = ""
    try:
        explicit = (headers.get("x-aiteam-source") or "").strip()
    except Exception:  # noqa: BLE001
        explicit = ""
    if explicit:
        return explicit
    ua = user_agent or ""
    if ua.startswith("Mozilla") or "Chrome" in ua or "Safari" in ua:
        return "dashboard"
    if "urllib" in ua or "python" in ua.lower() or "httpx" in ua or "curl" in ua:
        # Hooks and MCP tools share urllib; separating them needs the explicit
        # header above, which callers can opt into.
        return "hook-or-mcp"
    return "unknown"


def hour_bucket(now: datetime | None = None) -> str:
    return (now or datetime.now()).strftime("%Y-%m-%dT%H")


class RequestLedger:
    """In-memory counters + lazy hourly rollover into the event stream."""

    def __init__(self, event_bus) -> None:
        self._bus = event_bus
        self._bucket: str | None = None
        self._counts: dict[str, int] = {}

    def record(self, method: str, path_template: str, source: str, bucket: str) -> None:
        if self._bucket is None:
            self._bucket = bucket
        key = f"{method} {path_template}|{source}"
        self._counts[key] = self._counts.get(key, 0) + 1

    async def observe_bucket(self, bucket: str) -> None:
        """Called on every request; flushes the previous bucket when it rolls over."""
        if self._bucket is None or bucket == self._bucket:
            return
        await self._flush()
        self._bucket = bucket

    async def _flush(self) -> None:
        counts, bucket = self._counts, self._bucket
        # Clear first: a failing flush must not make the next one double-count.
        self._counts = {}
        if not counts:
            return
        endpoints = {key.split("|", 1)[0] for key in counts}
        try:
            await self._bus.emit(
                ROLLUP_EVENT,
                "api:request_ledger",
                {
                    "bucket": bucket,
                    "counts": counts,
                    "total": sum(counts.values()),
                    "distinct_endpoints": len(endpoints),
                },
            )
        except Exception:  # noqa: BLE001
            # Bookkeeping must never take down the request path it observes.
            logger.debug("request ledger flush failed", exc_info=True)


class _LazyBus:
    """Resolves the real EventBus at flush time.

    Middleware is wired at create_app(); the bus only exists after startup
    initialisation, so holding a reference here would capture ``None`` forever.
    """

    async def emit(self, event_type: str, source: str, data: dict) -> None:
        from aiteam.api.deps import get_event_bus

        await get_event_bus().emit(event_type, source, data)


class RequestLedgerMiddleware(BaseHTTPMiddleware):
    """Counts every API request by (method × route template × caller bucket)."""

    def __init__(self, app, ledger: RequestLedger | None = None) -> None:
        super().__init__(app)
        self._ledger = ledger or RequestLedger(_LazyBus())

    async def dispatch(self, request: Request, call_next) -> Response:
        path = request.url.path
        if any(path.startswith(p) for p in _SKIP_PREFIXES):
            return await call_next(request)

        response = await call_next(request)
        try:
            # The router fills scope["route"] during call_next, which is what
            # turns /api/tasks/<uuid> back into /api/tasks/{task_id}.
            route = request.scope.get("route")
            template = getattr(route, "path", None) or path
            source = classify_source(request.headers, request.headers.get("user-agent", ""))
            bucket = hour_bucket()
            self._ledger.record(request.method, template, source, bucket)
            await self._ledger.observe_bucket(bucket)
        except Exception:  # noqa: BLE001
            logger.debug("request ledger record failed", exc_info=True)
        return response
