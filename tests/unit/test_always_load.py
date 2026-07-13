"""工具渐进式加载 P1 — alwaysLoad 动态轮换单元测试。

覆盖三层：
1. 纯逻辑（compute_rotation / build_candidates）：跨天门槛下游、频次排序、硬顶、
   迟滞防抖（1.1x 不换 / 1.3x 换 / 在位者跌破门槛出局）、冷启动空数据。
2. 仓库 SQL（alwaysload_tool_frequencies）：跨天门槛挡单日爆发、频次降序、7 天窗口、前缀过滤。
3. 端点（GET /api/tools/always-load）：审计事件写入、迟滞基线连续性、失败静默返空。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

from aiteam.api import deps
from aiteam.api.always_load import (
    ALWAYSLOAD_TARGET,
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
    now = datetime.now()
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
    now = datetime.now()
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
    now = datetime.now()
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
    yield client, repo

    asyncio.get_event_loop().run_until_complete(close_db())
    deps._repository = None
    deps._memory_store = None
    deps._event_bus = None
    deps._manager = None
    deps._hook_translator = None


def _seed(repo: StorageRepository, tool_name: str, count_today: int, count_yesterday: int) -> None:
    now = datetime.now()
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


def test_endpoint_hysteresis_baseline_continuity(app_ctx):
    client, repo = app_ctx
    _seed(repo, "mcp__ai-team-os__a", 30, 30)
    _seed(repo, "mcp__ai-team-os__b", 20, 20)
    _seed(repo, "mcp__ai-team-os__c", 10, 10)
    reg = "registered=a,b,c"
    # 第一次：无基线 → 三个全为换入
    first = client.get(f"/api/tools/always-load?{reg}").json()
    assert set(first["tools"]) == {"a", "b", "c"}
    assert set(first["added"]) == {"a", "b", "c"}
    # 第二次：读上一条事件作在位者 → 无变化（换入换出皆空）
    second = client.get(f"/api/tools/always-load?{reg}").json()
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
    assert resp.json()["tools"] == []
