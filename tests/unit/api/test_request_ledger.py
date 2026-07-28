"""HTTP 请求级账本 — 观测断链闭合批 ⑤。

为什么需要它:此前"某工具零调用"的判断只有**一个**口径——CC 侧的 MCP 工具调用
观测。但 MCP 工具内部一律转成 HTTP 打到本地 API,Dashboard、hook、脚本、别的
会话也都走同一批 HTTP 端点。于是"零调用"实际只等于"这一个采集面没看见",
既排除不了采集断链,也排除不了口径隔离——正是 Council 纪律①「no-data≠zero」
点名的成因。补上请求级账本,零调用才有第二个口径可交叉验证。

设计约束:
* **不新建表**。I10 机检 ORM 声明表集合与实库一致,而生产实例不能重启建表;
  且事件流本就是 append-only 账本,聚合行天然属于它。
* **不造新噪声**。逐请求落库会把账本淹掉(本机 events 已 5 万+),故按
  「方法 × 路径模板 × 来源」在内存计数,**按小时**聚合成一条 rollup 事件。
* **不引定时器**。本仓刻意无后台守护/cron,故用**惰性翻滚**:下一个请求进来时
  发现跨桶了,才把上一桶落库。
* 路径用**模板**(/api/tasks/{task_id})而非实际路径,否则 id 会把基数打爆。
"""

from __future__ import annotations

import pytest

from aiteam.api.request_ledger import RequestLedger


class _Bus:
    def __init__(self):
        self.events: list[tuple[str, str, dict]] = []

    async def emit(self, event_type, source, data, **_kw):
        self.events.append((event_type, source, data))

    def rollups(self) -> list[dict]:
        return [d for t, _s, d in self.events if t == "api.request_rollup"]


@pytest.mark.asyncio()
async def test_counts_are_aggregated_not_per_request():
    """一小时内 30 次同类请求 → 落库 1 条 rollup,不是 30 条。"""
    bus = _Bus()
    ledger = RequestLedger(bus)
    for _ in range(30):
        ledger.record("GET", "/api/tasks/{task_id}", "mcp", bucket="2026-07-28T15")
    # 跨桶触发惰性翻滚
    await ledger.observe_bucket("2026-07-28T16")

    (rollup,) = bus.rollups()
    assert rollup["bucket"] == "2026-07-28T15"
    assert rollup["total"] == 30
    assert rollup["counts"]["GET /api/tasks/{task_id}|mcp"] == 30


@pytest.mark.asyncio()
async def test_dimensions_are_kept_separate():
    bus = _Bus()
    ledger = RequestLedger(bus)
    ledger.record("GET", "/api/tasks", "mcp", bucket="b1")
    ledger.record("POST", "/api/tasks", "mcp", bucket="b1")
    ledger.record("GET", "/api/tasks", "dashboard", bucket="b1")
    await ledger.observe_bucket("b2")

    (rollup,) = bus.rollups()
    assert rollup["counts"] == {
        "GET /api/tasks|mcp": 1,
        "POST /api/tasks|mcp": 1,
        "GET /api/tasks|dashboard": 1,
    }
    assert rollup["total"] == 3
    assert rollup["distinct_endpoints"] == 2


@pytest.mark.asyncio()
async def test_no_flush_while_inside_same_bucket():
    """同桶内不落库——否则又变成逐请求写。"""
    bus = _Bus()
    ledger = RequestLedger(bus)
    ledger.record("GET", "/api/health", "hook", bucket="b1")
    await ledger.observe_bucket("b1")
    assert bus.rollups() == []


@pytest.mark.asyncio()
async def test_empty_bucket_writes_nothing():
    """没有请求就不该留下空账页。"""
    bus = _Bus()
    ledger = RequestLedger(bus)
    await ledger.observe_bucket("b2")
    assert bus.rollups() == []


@pytest.mark.asyncio()
async def test_flush_is_idempotent():
    """翻滚后旧桶清空,重复翻滚不重复计数。"""
    bus = _Bus()
    ledger = RequestLedger(bus)
    ledger.record("GET", "/api/tasks", "mcp", bucket="b1")
    await ledger.observe_bucket("b2")
    await ledger.observe_bucket("b3")
    assert len(bus.rollups()) == 1


@pytest.mark.asyncio()
async def test_emit_failure_never_breaks_the_request_path():
    """账本落库失败绝不能把正在服务的请求带下水。"""

    class _Broken:
        async def emit(self, *_a, **_kw):
            raise RuntimeError("bus down")

    ledger = RequestLedger(_Broken())
    ledger.record("GET", "/api/tasks", "mcp", bucket="b1")
    await ledger.observe_bucket("b2")  # 不抛


def test_source_classification():
    """来源分桶:MCP 工具/hook 都走 urllib,浏览器走 UA;拿不准就 unknown。"""
    from aiteam.api.request_ledger import classify_source

    assert classify_source({"x-aiteam-source": "mcp"}, "") == "mcp"
    assert classify_source({}, "Mozilla/5.0 (Macintosh)") == "dashboard"
    assert classify_source({}, "Python-urllib/3.12") == "hook-or-mcp"
    assert classify_source({}, "") == "unknown"
