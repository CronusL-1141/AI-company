"""工具渐进式加载 P1 — alwaysLoad 动态轮换单元测试。

覆盖五层：
1. 纯逻辑（compute_rotation / build_candidates）：跨天门槛下游、频次排序、硬顶、
   迟滞防抖（1.1x 不换 / 1.3x 换 / 在位者跌破门槛出局）、冷启动空数据。
2. 仓库 SQL（alwaysload_tool_frequencies）：跨天门槛挡单日爆发、频次降序、7 天窗口、前缀过滤。
3. 端点（GET /api/tools/always-load）：审计事件写入、迟滞基线连续性、失败静默返空。
4. TTL 缓存：命中不查库不记事件、过期重算记事件、命中仍按 registered 过滤。
5. MCP server 侧挂载（apply_always_load_meta）：meta 必须真的出现在 tools/list 的
   `_meta` 里——断言跨 `to_mcp_tool()` 序列化边界，而不是只看内存对象被赋了值。
   这同时钉死「`list_tools()` 返回的是活组件而非副本」这条 fastmcp 行为假设。
   外加超时配置与落地回报事件（成功/超时两条路径的 payload）。
"""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request
from datetime import datetime, timedelta

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

from aiteam.api import deps
from aiteam.api.always_load import (
    ALWAYSLOAD_CACHE_TTL_S,
    ALWAYSLOAD_TARGET,
    APPLIED_EVENT_TYPE,
    APPLIED_REASONS,
    ROTATION_EVENT_TYPE,
    Candidate,
    build_candidates,
    compute_rotation,
    normalize_tool_name,
    parse_registered_param,
)
from aiteam.api.app import create_app
from aiteam.api.event_bus import EventBus
from aiteam.api.hook_translator import HookTranslator
from aiteam.api.routes import tools as tools_route
from aiteam.clock import utc_now
from aiteam.memory.store import MemoryStore
from aiteam.orchestrator.team_manager import TeamManager
from aiteam.storage.connection import close_db, get_session
from aiteam.storage.models import AgentActivityModel
from aiteam.storage.repository import StorageRepository
from aiteam.types import AgentActivity

# ============================================================
# Part A — 纯逻辑（无 I/O）
# ============================================================


def test_normalize_strips_prefix():
    assert normalize_tool_name("mcp__ai-team-os__task_memo_add") == "task_memo_add"
    # 无前缀原样返回
    assert normalize_tool_name("Bash") == "Bash"


def test_parse_registered_param():
    assert parse_registered_param("") is None
    assert parse_registered_param("a, b ,c") == {"a", "b", "c"}
    assert parse_registered_param("  ,  ") is None


def test_build_candidates_normalize_and_registered_filter():
    rows = [
        ("mcp__ai-team-os__task_memo_add", 100, 5),
        ("mcp__ai-team-os__memory_search", 40, 3),
        ("mcp__ai-team-os__ghost_tool", 30, 2),  # 已删工具，不在 registered
    ]
    registered = {"task_memo_add", "memory_search"}
    cands = build_candidates(rows, registered)
    assert [c.name for c in cands] == ["task_memo_add", "memory_search"]
    # 频次降序
    assert cands[0].count == 100 and cands[1].count == 40


def test_build_candidates_no_registration_filter():
    rows = [("mcp__ai-team-os__foo", 10, 2)]
    cands = build_candidates(rows, None)
    assert [c.name for c in cands] == ["foo"]


def test_compute_rotation_hard_cap_and_target():
    # 6 个合格候选，target=3 → 只留频次最高的 3 个
    cands = [Candidate(f"t{i}", count=100 - i, days=3) for i in range(6)]
    result = compute_rotation(cands, incumbents=[])
    assert len(result.tools) == ALWAYSLOAD_TARGET
    assert result.names == ["t0", "t1", "t2"]
    assert result.added == ["t0", "t1", "t2"]
    assert result.removed == []


def test_compute_rotation_cold_start_empty():
    result = compute_rotation([], incumbents=[])
    assert result.names == []
    assert result.added == []
    assert result.removed == []


def test_compute_rotation_data_insufficient_no_padding():
    # 只有 2 个合格 → 返回 2 个，不凑够 3
    cands = [Candidate("a", 50, 3), Candidate("b", 40, 2)]
    result = compute_rotation(cands, incumbents=[])
    assert result.names == ["a", "b"]


def test_hysteresis_challenger_1_1x_no_swap():
    # 槽位被 3 个在位者占满；挑战者 d 频次 = 最弱在位者 ×1.1，不足 1.2x → 不换
    cands = [
        Candidate("a", 300, 5),
        Candidate("b", 200, 5),
        Candidate("c", 100, 5),  # 最弱在位者
        Candidate("d", 110, 3),  # 挑战者 110 = 100×1.1 < 100×1.2
    ]
    result = compute_rotation(cands, incumbents=["a", "b", "c"])
    assert set(result.names) == {"a", "b", "c"}
    assert result.added == []
    assert result.removed == []


def test_hysteresis_challenger_1_3x_swap():
    # 挑战者 d 频次 = 最弱在位者 ×1.3 > 1.2x → 顶替最弱在位者 c
    cands = [
        Candidate("a", 300, 5),
        Candidate("b", 200, 5),
        Candidate("c", 100, 5),
        Candidate("d", 130, 3),
    ]
    result = compute_rotation(cands, incumbents=["a", "b", "c"])
    assert set(result.names) == {"a", "b", "d"}
    assert result.added == ["d"]
    assert result.removed == ["c"]


def test_incumbent_drops_below_threshold_removed():
    # 在位者 c 本期不合格（不在候选中，跨天门槛已挡）→ 出局，空槽由挑战者 d 直接补入
    cands = [
        Candidate("a", 300, 5),
        Candidate("b", 200, 5),
        Candidate("d", 50, 3),  # 挑战者，频次低但有空槽可直接进
    ]
    result = compute_rotation(cands, incumbents=["a", "b", "c"])
    assert set(result.names) == {"a", "b", "d"}
    assert result.added == ["d"]
    assert result.removed == ["c"]


def test_incumbent_full_slots_weak_challenger_stays_out():
    # 所有在位者仍合格且占满槽位，挑战者顶不动 → 名单不变
    cands = [
        Candidate("a", 300, 5),
        Candidate("b", 200, 5),
        Candidate("c", 100, 5),
        Candidate("d", 90, 3),
        Candidate("e", 80, 3),
    ]
    result = compute_rotation(cands, incumbents=["a", "b", "c"])
    assert set(result.names) == {"a", "b", "c"}
    assert result.added == [] and result.removed == []


# ============================================================
# Part B — 仓库 SQL
# ============================================================


@pytest_asyncio.fixture()
async def repo() -> StorageRepository:
    r = StorageRepository(db_url="sqlite+aiosqlite://")
    await r.init_db()
    yield r  # type: ignore[misc]
    await close_db()


async def _insert_activity(
    repo: StorageRepository,
    tool_name: str,
    ts: datetime,
    agent_id: str = "agent-1",
) -> None:
    """直插一条 agent_activities，带指定时间戳（create_activity 不支持自定义时间）。"""
    activity = AgentActivity(
        agent_id=agent_id,
        session_id="sess-1",
        tool_name=tool_name,
        timestamp=ts,
    )
    orm = AgentActivityModel.from_pydantic(activity)
    async with get_session(repo._db_url) as session:
        session.add(orm)


async def test_sql_cross_day_threshold_blocks_single_day_burst(repo: StorageRepository):
    now = utc_now()
    # burst：同一天 10 次 → 跨天数=1，被挡
    for _ in range(10):
        await _insert_activity(repo, "mcp__ai-team-os__burst_tool", now)
    # spread：跨 2 天各 1 次 → 跨天数=2，入选
    await _insert_activity(repo, "mcp__ai-team-os__spread_tool", now)
    await _insert_activity(repo, "mcp__ai-team-os__spread_tool", now - timedelta(days=1))

    rows = await repo.alwaysload_tool_frequencies()
    names = [r[0] for r in rows]
    assert "mcp__ai-team-os__spread_tool" in names
    assert "mcp__ai-team-os__burst_tool" not in names


async def test_sql_frequency_desc_order(repo: StorageRepository):
    now = utc_now()
    yesterday = now - timedelta(days=1)
    # high：跨 2 天共 4 次
    for ts in (now, now, yesterday, yesterday):
        await _insert_activity(repo, "mcp__ai-team-os__high", ts)
    # low：跨 2 天共 2 次
    for ts in (now, yesterday):
        await _insert_activity(repo, "mcp__ai-team-os__low", ts)

    rows = await repo.alwaysload_tool_frequencies()
    assert rows[0][0] == "mcp__ai-team-os__high"
    assert rows[0][1] == 4
    assert rows[1][0] == "mcp__ai-team-os__low"


async def test_sql_seven_day_window_and_prefix_filter(repo: StorageRepository):
    now = utc_now()
    # 8 天前的活动 → 超窗，排除
    await _insert_activity(repo, "mcp__ai-team-os__stale", now - timedelta(days=8))
    await _insert_activity(repo, "mcp__ai-team-os__stale", now - timedelta(days=9))
    # 非 mcp 前缀 → 前缀过滤排除（即使跨天合格）
    await _insert_activity(repo, "Bash", now)
    await _insert_activity(repo, "Bash", now - timedelta(days=1))

    rows = await repo.alwaysload_tool_frequencies()
    names = [r[0] for r in rows]
    assert "mcp__ai-team-os__stale" not in names
    assert "Bash" not in names


# ============================================================
# Part C — 端点
# ============================================================


@pytest.fixture()
def app_ctx():
    """内存 SQLite 的 TestClient + repo。"""
    repo = StorageRepository(db_url="sqlite+aiosqlite://")
    asyncio.get_event_loop().run_until_complete(repo.init_db())
    memory = MemoryStore(repository=repo)
    manager = TeamManager(repository=repo, memory=memory)
    event_bus = EventBus(repo=repo)
    hook_translator = HookTranslator(repo=repo, event_bus=event_bus)
    deps._repository = repo
    deps._memory_store = memory
    deps._event_bus = event_bus
    deps._manager = manager
    deps._hook_translator = hook_translator

    app = create_app()

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def test_lifespan(app):
        yield

    app.router.lifespan_context = test_lifespan
    client = TestClient(app)
    # 轮换缓存是模块级状态，跨用例会串味（上一个用例算出的名单会被下一个用例当
    # 缓存命中取走）。每个用例进出各清一次。
    tools_route.reset_always_load_cache()
    yield client, repo
    tools_route.reset_always_load_cache()

    asyncio.get_event_loop().run_until_complete(close_db())
    deps._repository = None
    deps._memory_store = None
    deps._event_bus = None
    deps._manager = None
    deps._hook_translator = None


def _seed(repo: StorageRepository, tool_name: str, count_today: int, count_yesterday: int) -> None:
    now = utc_now()
    yesterday = now - timedelta(days=1)
    loop = asyncio.get_event_loop()
    for _ in range(count_today):
        loop.run_until_complete(_insert_activity(repo, tool_name, now))
    for _ in range(count_yesterday):
        loop.run_until_complete(_insert_activity(repo, tool_name, yesterday))


def test_endpoint_cold_start_empty_and_audit_written(app_ctx):
    client, repo = app_ctx
    resp = client.get("/api/tools/always-load")
    assert resp.status_code == 200
    body = resp.json()
    assert body["tools"] == []
    # 冷启动也落一行审计事件（同时是下期迟滞基线）
    ev = client.get(f"/api/events?type={ROTATION_EVENT_TYPE}&limit=5")
    assert ev.status_code == 200
    assert ev.json()["total"] >= 1


def test_endpoint_computes_and_writes_named_result(app_ctx):
    client, repo = app_ctx
    _seed(repo, "mcp__ai-team-os__task_memo_add", 5, 5)
    _seed(repo, "mcp__ai-team-os__memory_search", 3, 3)
    resp = client.get(
        "/api/tools/always-load?registered=task_memo_add,memory_search"
    )
    body = resp.json()
    assert set(body["tools"]) == {"task_memo_add", "memory_search"}
    assert set(body["added"]) == {"task_memo_add", "memory_search"}

    # 审计事件的 data.tools 名字与结果一致
    ev = client.get(f"/api/events?type={ROTATION_EVENT_TYPE}&limit=1").json()
    tools_data = ev["data"][0]["data"]["tools"]
    assert {t["name"] for t in tools_data} == {"task_memo_add", "memory_search"}


def test_endpoint_hysteresis_baseline_continuity(app_ctx, fake_clock):
    client, repo = app_ctx
    _seed(repo, "mcp__ai-team-os__a", 30, 30)
    _seed(repo, "mcp__ai-team-os__b", 20, 20)
    _seed(repo, "mcp__ai-team-os__c", 10, 10)
    reg = "registered=a,b,c"
    # 第一次：无基线 → 三个全为换入
    first = client.get(f"/api/tools/always-load?{reg}").json()
    assert set(first["tools"]) == {"a", "b", "c"}
    assert set(first["added"]) == {"a", "b", "c"}
    # 第二次必须是真重算才谈得上"读上一条事件作基线"，所以先把缓存推过期。
    fake_clock.advance(ALWAYSLOAD_CACHE_TTL_S + 1)
    second = client.get(f"/api/tools/always-load?{reg}").json()
    assert second["cached"] is False
    assert set(second["tools"]) == {"a", "b", "c"}
    assert second["added"] == []
    assert second["removed"] == []


def test_endpoint_failure_returns_empty_silently(app_ctx, monkeypatch):
    client, repo = app_ctx

    async def _boom(*args, **kwargs):
        raise RuntimeError("db exploded")

    monkeypatch.setattr(repo, "alwaysload_tool_frequencies", _boom)
    resp = client.get("/api/tools/always-load")
    assert resp.status_code == 200
    body = resp.json()
    assert body["tools"] == []
    assert body["cached"] is False
    assert body["computed_at"] is None


def test_endpoint_failure_does_not_poison_cache(app_ctx, monkeypatch):
    """失败不写缓存——否则一次抖动会把空名单钉死整个 TTL。"""
    client, repo = app_ctx
    _seed(repo, "mcp__ai-team-os__a", 5, 5)

    real = repo.alwaysload_tool_frequencies

    async def _boom(*args, **kwargs):
        raise RuntimeError("db exploded")

    monkeypatch.setattr(repo, "alwaysload_tool_frequencies", _boom)
    assert client.get("/api/tools/always-load?registered=a").json()["tools"] == []

    monkeypatch.setattr(repo, "alwaysload_tool_frequencies", real)
    body = client.get("/api/tools/always-load?registered=a").json()
    assert body["tools"] == ["a"]
    assert body["cached"] is False


# ============================================================
# Part C2 — TTL 缓存
# ============================================================


class _FakeClock:
    """可控单调时钟，替换 routes.tools._monotonic。"""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture()
def fake_clock(monkeypatch):
    clock = _FakeClock()
    monkeypatch.setattr(tools_route, "_monotonic", clock)
    return clock


def _rotation_count(client) -> int:
    # limit 必须大于期望条数：/api/events 的 total 是本页行数，不是全表计数。
    return client.get(f"/api/events?type={ROTATION_EVENT_TYPE}&limit=50").json()["total"]


def test_cache_hit_within_ttl_records_no_rotation_event(app_ctx, fake_clock):
    """TTL 内第二次调用直接复用快照：不查库、不记事件、cached=true。"""
    client, repo = app_ctx
    _seed(repo, "mcp__ai-team-os__a", 5, 5)
    reg = "registered=a"

    first = client.get(f"/api/tools/always-load?{reg}").json()
    assert first["cached"] is False
    assert first["tools"] == ["a"]
    after_first = _rotation_count(client)

    # 明确断言"没再查库"，而不是只看事件数——事件数相等也可能是查了库没写成。
    async def _must_not_run(*args, **kwargs):
        raise AssertionError("缓存命中却仍查了库")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(repo, "alwaysload_tool_frequencies", _must_not_run)
        fake_clock.advance(ALWAYSLOAD_CACHE_TTL_S - 1)
        second = client.get(f"/api/tools/always-load?{reg}").json()

    assert second["cached"] is True
    assert second["tools"] == ["a"]
    assert second["computed_at"] == first["computed_at"]
    assert second["added"] == [] and second["removed"] == []
    assert _rotation_count(client) == after_first


def test_cache_expiry_recomputes_and_records_event(app_ctx, fake_clock):
    """TTL 过期后重算并再落一行审计事件。"""
    client, repo = app_ctx
    _seed(repo, "mcp__ai-team-os__a", 5, 5)
    reg = "registered=a"

    first = client.get(f"/api/tools/always-load?{reg}").json()
    after_first = _rotation_count(client)

    fake_clock.advance(ALWAYSLOAD_CACHE_TTL_S + 1)
    second = client.get(f"/api/tools/always-load?{reg}").json()

    assert second["cached"] is False
    assert second["computed_at"] != first["computed_at"]
    assert _rotation_count(client) == after_first + 1


def test_cache_hit_still_filters_by_registered(app_ctx, fake_clock):
    """快照可能是别的实例算的，命中时仍须按本次调用方的在册工具过滤。"""
    client, repo = app_ctx
    _seed(repo, "mcp__ai-team-os__a", 30, 30)
    _seed(repo, "mcp__ai-team-os__b", 20, 20)

    first = client.get("/api/tools/always-load?registered=a,b").json()
    assert set(first["tools"]) == {"a", "b"}

    fake_clock.advance(1)
    second = client.get("/api/tools/always-load?registered=a").json()
    assert second["cached"] is True
    assert second["tools"] == ["a"]
    assert [d["name"] for d in second["detail"]] == ["a"]


# ============================================================
# Part D — MCP server 侧挂载
# ============================================================


class _CannedResponse:
    """urlopen 的最小替身：空 JSON 体，支持 with 语句。"""

    def __init__(self, body: bytes = b"{}") -> None:
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self) -> bytes:
        return self._body


@pytest.fixture(autouse=True)
def captured_http(monkeypatch):
    """拦下本模块所有 urllib 出站请求并记录下来。

    不拦就会真的打到本机在跑的 API 上、往生产库写事件——单测不得碰生产库。做成
    autouse 是为了不依赖"新加用例记得自己拦"；需要自定义行为的用例再各自覆盖。
    """
    calls: list[dict] = []

    def _fake_urlopen(req, timeout=None):
        body = None
        if getattr(req, "data", None):
            body = json.loads(req.data.decode("utf-8"))
        calls.append(
            {
                "url": req.full_url,
                "method": req.get_method(),
                "timeout": timeout,
                "body": body,
            }
        )
        return _CannedResponse()

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)
    return calls


def _applied_reports(calls: list[dict]) -> list[dict]:
    """从记录里筛出落地回报的 POST body。"""
    return [
        c["body"] for c in calls if c["url"].endswith("/api/tools/always-load/applied")
    ]


def _tiny_server():
    """两个工具的迷你 FastMCP，用于验证 meta 挂载。"""
    from fastmcp import FastMCP

    server = FastMCP(name="alwaysload-test")

    @server.tool
    def winner(a: int) -> str:
        """Winner tool.

        Args:
            a: number
        """
        return "w"

    @server.tool
    def loser(a: int) -> str:
        """Loser tool.

        Args:
            a: number
        """
        return "l"

    return server


def _mcp_meta(server, tool_name: str) -> dict:
    """取工具经 to_mcp_tool() 序列化后的 _meta —— 客户端真正看到的那份。"""
    tools = asyncio.run(server.list_tools())
    tool = next(t for t in tools if t.name == tool_name)
    return tool.to_mcp_tool().meta or {}


def test_apply_meta_lands_in_serialized_tool(monkeypatch):
    """挂上的 meta 必须跨 to_mcp_tool() 边界存活，否则 CC 侧看不到豁免。"""
    from aiteam.mcp import _alwaysload

    monkeypatch.setattr(_alwaysload, "_fetch_always_load", lambda registered: (["winner"], ""))
    server = _tiny_server()

    tagged = _alwaysload.apply_always_load_meta(server)

    assert tagged == ["winner"]
    assert _mcp_meta(server, "winner").get(_alwaysload.ALWAYSLOAD_META_KEY) is True
    assert _alwaysload.ALWAYSLOAD_META_KEY not in _mcp_meta(server, "loser")


def test_apply_meta_passes_registered_names_to_api(monkeypatch):
    """传给 API 的 registered 必须是实际在册的裸工具名。"""
    from aiteam.mcp import _alwaysload

    seen: list[list[str]] = []

    def _capture(registered):
        seen.append(sorted(registered))
        return [], ""

    monkeypatch.setattr(_alwaysload, "_fetch_always_load", _capture)
    assert _alwaysload.apply_always_load_meta(_tiny_server()) == []
    assert seen == [["loser", "winner"]]


def test_apply_meta_preserves_existing_meta(monkeypatch):
    """已有 meta 是合并不是覆盖。"""
    from aiteam.mcp import _alwaysload

    monkeypatch.setattr(_alwaysload, "_fetch_always_load", lambda registered: (["winner"], ""))
    server = _tiny_server()
    tool = next(t for t in asyncio.run(server.list_tools()) if t.name == "winner")
    tool.meta = {"keep": "me"}

    _alwaysload.apply_always_load_meta(server)

    meta = _mcp_meta(server, "winner")
    assert meta.get("keep") == "me"
    assert meta.get(_alwaysload.ALWAYSLOAD_META_KEY) is True


def test_apply_meta_noop_when_api_returns_nothing(monkeypatch):
    """API 返回空名单（服务未起/超时也走这条）→ 一个工具都不挂，全 defer。"""
    from aiteam.mcp import _alwaysload

    monkeypatch.setattr(_alwaysload, "_fetch_always_load", lambda registered: ([], ""))
    server = _tiny_server()
    assert _alwaysload.apply_always_load_meta(server) == []
    assert _alwaysload.ALWAYSLOAD_META_KEY not in _mcp_meta(server, "winner")


# ============================================================
# Part E — 客户端超时配置与落地回报
# ============================================================


def test_fetch_timeout_default(monkeypatch):
    from aiteam.mcp import _alwaysload

    monkeypatch.delenv(_alwaysload._TIMEOUT_ENV, raising=False)
    assert _alwaysload._fetch_timeout_s() == _alwaysload._TIMEOUT_S
    # 默认值必须覆盖实测的冷启动延迟（1.6~4.6s 的下半段），又留在 CC 等工具列表的
    # 5s 上限之内——两头都钉住，改动任一端都会在这里绊住。
    assert 3.0 <= _alwaysload._TIMEOUT_S <= 5.0


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("7.5", 7.5),
        ("10", 10.0),
        ("  4.25  ", 4.25),
    ],
)
def test_fetch_timeout_env_override(monkeypatch, raw, expected):
    from aiteam.mcp import _alwaysload

    monkeypatch.setenv(_alwaysload._TIMEOUT_ENV, raw)
    assert _alwaysload._fetch_timeout_s() == expected


@pytest.mark.parametrize("raw", ["", "   ", "abc", "0", "-3", "nan_but_not"])
def test_fetch_timeout_env_invalid_falls_back(monkeypatch, raw):
    """非法覆盖值一律回落默认——启动路径上不接受"配错就挂死"。"""
    from aiteam.mcp import _alwaysload

    monkeypatch.setenv(_alwaysload._TIMEOUT_ENV, raw)
    assert _alwaysload._fetch_timeout_s() == _alwaysload._TIMEOUT_S


def test_fetch_timeout_env_reaches_urlopen(monkeypatch, captured_http):
    """覆盖值必须真的传进 urlopen，而不是只在配置函数里成立。"""
    from aiteam.mcp import _alwaysload

    monkeypatch.setenv(_alwaysload._TIMEOUT_ENV, "6.5")
    assert _alwaysload._fetch_always_load(["a"]) == ([], "")
    assert [c["timeout"] for c in captured_http] == [6.5]


@pytest.mark.parametrize(
    ("exc", "expected_reason"),
    [
        (TimeoutError("read timed out"), "timeout"),
        (urllib.error.URLError(TimeoutError()), "timeout"),
        (urllib.error.URLError(ConnectionRefusedError()), "no_api"),
        (urllib.error.HTTPError("http://x", 500, "boom", {}, None), "http_error"),
        (ValueError("bad json"), "http_error"),
    ],
)
def test_fetch_reason_classification(monkeypatch, exc, expected_reason):
    """四值原因必须分得开，否则「启动期零常驻」又变成一句静默。"""
    from aiteam.mcp import _alwaysload

    def _raise(req, timeout=None):
        raise exc

    monkeypatch.setattr(urllib.request, "urlopen", _raise)
    tools, reason = _alwaysload._fetch_always_load(["a"])
    assert tools == []
    assert reason == expected_reason


def test_client_reasons_match_server_closed_set():
    """客户端产出的原因值必须落在服务端接受的闭集内（两侧隔着一条 HTTP，靠这条钉住）。"""
    from aiteam.mcp import _alwaysload

    client_reasons = {
        _alwaysload._REASON_OK,
        _alwaysload._REASON_TIMEOUT,
        _alwaysload._REASON_HTTP_ERROR,
        _alwaysload._REASON_NO_API,
    }
    assert client_reasons == set(APPLIED_REASONS)


def test_applied_report_on_success_path(monkeypatch, captured_http):
    """成功路径：回报挂上的工具名、非负耗时、原因为空串。"""
    from aiteam.mcp import _alwaysload

    monkeypatch.setattr(_alwaysload, "_fetch_always_load", lambda registered: (["winner"], ""))
    _alwaysload.apply_always_load_meta(_tiny_server())

    reports = _applied_reports(captured_http)
    assert len(reports) == 1
    assert reports[0]["tools"] == ["winner"]
    assert reports[0]["reason"] == ""
    assert isinstance(reports[0]["elapsed_ms"], int) and reports[0]["elapsed_ms"] >= 0


def test_applied_report_on_timeout_path(monkeypatch, captured_http):
    """超时路径：名单为空但原因必须是 timeout —— 这正是旧实现看不出来的那个失败。"""
    from aiteam.mcp import _alwaysload

    monkeypatch.setattr(
        _alwaysload, "_fetch_always_load", lambda registered: ([], _alwaysload._REASON_TIMEOUT)
    )
    _alwaysload.apply_always_load_meta(_tiny_server())

    reports = _applied_reports(captured_http)
    assert len(reports) == 1
    assert reports[0]["tools"] == []
    assert reports[0]["reason"] == "timeout"


def test_applied_report_posts_to_endpoint(captured_http):
    """回报走 POST /api/tools/always-load/applied，body 三字段齐备。"""
    from aiteam.mcp import _alwaysload

    _alwaysload._post_applied_event(["a"], 42, "timeout")

    assert len(captured_http) == 1
    call = captured_http[0]
    assert call["url"].endswith("/api/tools/always-load/applied")
    assert call["method"] == "POST"
    assert call["timeout"] == _alwaysload._REPORT_TIMEOUT_S
    assert call["body"] == {"tools": ["a"], "elapsed_ms": 42, "reason": "timeout"}


def test_applied_report_failure_is_silent(monkeypatch):
    """API 不在时回报失败不得抛——这条 POST 在会话启动路径上。"""
    from aiteam.mcp import _alwaysload

    def _raise(req, timeout=None):
        raise OSError("connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", _raise)
    _alwaysload._post_applied_event(["a"], 1, "no_api")  # 不抛即通过


def test_applied_endpoint_records_event(app_ctx):
    """服务端把回报落成一行 tool.alwaysload.applied 事件。"""
    client, _repo = app_ctx
    resp = client.post(
        "/api/tools/always-load/applied",
        json={"tools": ["task_memo_add"], "elapsed_ms": 120, "reason": ""},
    )
    assert resp.status_code == 200
    assert resp.json()["recorded"] is True

    ev = client.get(f"/api/events?type={APPLIED_EVENT_TYPE}&limit=1").json()
    assert ev["total"] == 1
    data = ev["data"][0]["data"]
    assert data["tools"] == ["task_memo_add"]
    assert data["count"] == 1
    assert data["elapsed_ms"] == 120
    assert data["reason"] == ""


def test_applied_endpoint_rejects_unknown_reason(app_ctx):
    """原因是闭集，写不进自由文本。"""
    client, _repo = app_ctx
    resp = client.post(
        "/api/tools/always-load/applied",
        json={"tools": [], "elapsed_ms": 0, "reason": "whatever"},
    )
    assert resp.status_code == 422
