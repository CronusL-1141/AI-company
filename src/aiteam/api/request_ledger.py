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

hook 入库计数（``HOOK_INGEST_COUNTERS``）搭同一条 rollup：接收端的这些结局 access log
里看不到（客户端已断开，uvicorn 不写响应行），进程内计数一重启就归零，所以每小时随
rollup 落库、进程退出时（lifespan 收尾与 HTTP 硬退两条路径）再补落一次，os_health_check
经 ``GET /api/hooks/ingest-stats`` 读近 24 小时的和。进程被强杀时最后一段没落库，读侧据此
标 ``complete=false``。
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from aiteam.clock import utc_now

logger = logging.getLogger(__name__)

ROLLUP_EVENT = "api.request_rollup"

# Hook ingest outcomes counted by the concurrency middleware and the hook route
# (meanings in ``middleware.hook_ingest_stats``), rolled up with the request counts.
HOOK_INGEST_COUNTERS = (
    "client_gone", "body_lost", "slow", "replayed", "replay_duplicate", "replay_skipped",
)

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
    return (now or utc_now()).strftime("%Y-%m-%dT%H")


class RequestLedger:
    """In-memory counters + lazy hourly rollover into the event stream."""

    def __init__(self, event_bus) -> None:
        self._bus = event_bus
        self._bucket: str | None = None
        self._counts: dict[str, int] = {}
        # Hook ingest counts by bucket, not yet rolled up. Keyed by bucket because
        # they can arrive for an hour no request has opened yet (a body lost before
        # the ledger middleware ever sees the request).
        self._ingest: dict[str, dict[str, int]] = {}
        self.pid = os.getpid()
        self.started_at = utc_now()
        self.ingest_since_start = dict.fromkeys(HOOK_INGEST_COUNTERS, 0)
        # Set by mark_opened: the first rollover writes a row even for an empty
        # bucket, so a process idle since startup becomes visible again in the
        # hour its traffic resumes (see summarize_hook_ingest).
        self._announce = False

    def record(self, method: str, path_template: str, source: str, bucket: str) -> None:
        if self._bucket is None:
            self._bucket = bucket
        key = f"{method} {path_template}|{source}"
        self._counts[key] = self._counts.get(key, 0) + 1

    def note_hook_ingest(self, name: str, bucket: str | None = None) -> None:
        """Count one hook ingest outcome; it is rolled up with its hour's requests."""
        bucket = bucket or hour_bucket()
        counts = self._ingest.setdefault(bucket, {})
        counts[name] = counts.get(name, 0) + 1
        self.ingest_since_start[name] = self.ingest_since_start.get(name, 0) + 1

    def pending_hook_ingest(self) -> dict[str, int]:
        """Hook ingest counts not rolled up yet, summed over their buckets."""
        totals = dict.fromkeys(HOOK_INGEST_COUNTERS, 0)
        for counts in self._ingest.values():
            for name, value in counts.items():
                totals[name] = totals.get(name, 0) + value
        return totals

    async def observe_bucket(self, bucket: str) -> None:
        """Called on every request; flushes the buckets that bucket has rolled past."""
        stale = sorted(b for b in {self._bucket, *self._ingest} if b is not None and b < bucket)
        if not stale:
            return
        for old in stale:
            await self._flush(old)
        self._bucket = bucket

    async def mark_opened(self) -> None:
        """Write an empty rollup naming this process; called once at startup.

        A process killed before any hourly rollup would otherwise leave no trace,
        and its lost counts would read as zero. With the marker it shows up as a
        process without a ``final`` rollup, so the read side reports the window
        as incomplete.
        """
        bucket = hour_bucket()
        if self._bucket is None:
            self._bucket = bucket
        self._announce = True
        try:
            await self._bus.emit(ROLLUP_EVENT, "api:request_ledger", {
                "bucket": bucket, "counts": {}, "total": 0, "distinct_endpoints": 0,
                "pid": self.pid, "process_started_at": self.started_at.isoformat(),
                "opened": True,
            })
        except Exception:  # noqa: BLE001
            # Without the marker this process is invisible to the read side until
            # its first rollup: killed before that, its lost counts read as zero.
            logger.warning("request ledger open marker failed", exc_info=True)

    async def flush_all(self) -> None:
        """Roll up everything still in memory, open bucket included; called on exit.

        Always writes one ``final`` rollup, even an empty one: it is how the read
        side tells a process that exited cleanly from one killed before its last
        counts reached the database.
        """
        buckets = sorted(b for b in {self._bucket, *self._ingest} if b is not None)
        for bucket in buckets[:-1]:
            await self._flush(bucket)
        await self._flush(buckets[-1] if buckets else hour_bucket(), final=True)

    async def _flush(self, bucket: str, *, final: bool = False) -> None:
        counts = self._counts if bucket == self._bucket else {}
        ingest = self._ingest.pop(bucket, {})
        # Clear first: a failing flush must not make the next one double-count.
        if bucket == self._bucket:
            self._counts = {}
        if not counts and not ingest and not final and not self._announce:
            return
        self._announce = False
        endpoints = {key.split("|", 1)[0] for key in counts}
        data = {
            "bucket": bucket,
            "counts": counts,
            "total": sum(counts.values()),
            "distinct_endpoints": len(endpoints),
            "pid": self.pid,
            "process_started_at": self.started_at.isoformat(),
        }
        if ingest:
            data["hook_ingest"] = ingest
        if final:
            data["final"] = True
        try:
            await self._bus.emit(ROLLUP_EVENT, "api:request_ledger", data)
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


INGEST_NOTES = {
    "client_gone": (
        "lower bound: receipts lost while the event queued for a DB slot (the event itself "
        "was handled); a close that uvicorn had not yet processed when the slot came is not counted"
    ),
    "body_lost": (
        "events lost: the client left before the body was read, uvicorn then discards it; "
        "only the client-side replay queue recovers these"
    ),
    "slow": "hook requests over 1s from arrival to response (queue + handler)",
}


def summarize_hook_ingest(
    rollups: list[tuple[datetime, dict]], ledger: RequestLedger, since: datetime,
) -> dict:
    """Hook ingest counts over a window: rolled-up hours plus what is still in memory.

    ``complete`` is false when a process seen in the window has no ``final``
    rollup (killed before its exit flush, or a second API instance still running):
    its counts since its last hourly rollup are missing, so the sums are a lower
    bound.

    Counts are selected by bucket, processes by when their rows were written, from
    an hour before ``since`` on. A rollover writes an hour's row during the next
    hour, and the ledger writes a row at startup and at its first rollover (even an
    empty one), so a process with counts in the window has a row in that range
    (short of an hour whose only traffic is lost bodies, which never reach the
    ledger middleware to roll the hour over). Selecting processes by bucket missed
    one whose only recent row was the rollover of the hour before the window.
    Erring the other way, a process killed in the hour before the window marks it
    incomplete.
    """
    since_bucket = hour_bucket(since)
    seen_since = since - timedelta(hours=1)
    window = dict.fromkeys(HOOK_INGEST_COUNTERS, 0)
    processes: dict[tuple, bool] = {}
    for at, data in rollups:
        key = (data.get("pid"), data.get("process_started_at"))
        if key != (None, None) and at >= seen_since:
            processes[key] = processes.get(key, False) or bool(data.get("final"))
        if str(data.get("bucket", "")) < since_bucket:
            continue
        for name, value in (data.get("hook_ingest") or {}).items():
            if isinstance(value, int):
                window[name] = window.get(name, 0) + value
    for name, value in ledger.pending_hook_ingest().items():
        window[name] = window.get(name, 0) + value
    current = (ledger.pid, ledger.started_at.isoformat())
    unflushed = sum(1 for key, final in processes.items() if not final and key != current)
    return {
        "since": since.isoformat(),
        "window": window,
        "complete": unflushed == 0,
        "processes_without_final_rollup": unflushed,
        "current_process": {
            "pid": ledger.pid,
            "started_at": ledger.started_at.isoformat(),
            "counts": dict(ledger.ingest_since_start),
        },
        "notes": INGEST_NOTES,
    }


# The process-wide ledger: one per API process, so its counts can be flushed on
# exit and read by the ingest-stats route.
request_ledger = RequestLedger(_LazyBus())


async def flush_request_ledger() -> None:
    """Exit-path flush of the process ledger (lifespan path); never raises.

    Bounded by ``exit_writes``: on a locked database the flush is left behind
    rather than holding the exit, its counts are then lost, and the missing
    ``final`` rollup makes the read side report the window as incomplete.
    """
    from aiteam.api.exit_writes import write_or_abandon

    await write_or_abandon({"request_ledger": request_ledger.flush_all()})


class RequestLedgerMiddleware(BaseHTTPMiddleware):
    """Counts every API request by (method × route template × caller bucket)."""

    def __init__(self, app, ledger: RequestLedger | None = None) -> None:
        super().__init__(app)
        self._ledger = ledger or request_ledger

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
            # Roll the previous hour first, so the first request of a new hour is
            # counted in its own hour, not in the one being rolled up.
            await self._ledger.observe_bucket(bucket)
            self._ledger.record(request.method, template, source, bucket)
        except Exception:  # noqa: BLE001
            logger.debug("request ledger record failed", exc_info=True)
        return response
