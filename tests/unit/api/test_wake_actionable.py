"""Unit tests for the wake actionable predicate (唤醒体系 v2 §7.1/§7.2).

判据逻辑用轻量 FakeRepo 精测每条分支；再用 TestClient 对真实路由做一次冒烟，
证明接线正确且空库不 500（防御式契约）。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from aiteam.api import wake_actionable
from aiteam.types import AgentStatus

NOW = datetime(2026, 7, 14, 12, 0, 0)
BEFORE = NOW - timedelta(minutes=30)
AFTER = NOW + timedelta(minutes=1)
SINCE = NOW.isoformat()


def _agent(status, name="a", last_active=None):
    return SimpleNamespace(status=status, name=name, last_active_at=last_active)


def _run(status, session_id="sid", updated_at=None, wf_id="wf_x"):
    return SimpleNamespace(
        status=status, session_id=session_id, updated_at=updated_at, wf_id=wf_id
    )


class FakeRepo:
    """只实现 compute_actionable 用到的 6 个方法。"""

    def __init__(
        self,
        agents=None,
        runs=None,
        memos_since=0,
        briefings=0,
        team_project="proj-1",
    ):
        self._agents = agents or []
        self._runs = runs or []
        self._memos_since = memos_since
        self._briefings = briefings
        self._team_project = team_project

    async def get_team(self, team_id):
        return SimpleNamespace(project_id=self._team_project)

    async def list_agents(self, team_id):
        return list(self._agents)

    async def find_agents_by_session(self, session_id):
        return list(self._agents)

    async def list_workflow_runs(self, project_id="", limit=50):
        return list(self._runs)

    async def count_valid_task_memos_since(self, project_id, since):
        return self._memos_since

    async def list_briefings(self, status="pending", project_id=""):
        return [object()] * self._briefings


async def _compute(repo, **kw):
    kw.setdefault("session_id", "sid")
    kw.setdefault("team_id", "team-1")
    kw.setdefault("since_raw", SINCE)
    return await wake_actionable.compute_actionable(repo, **kw)


# ---- parse_since ----------------------------------------------------------
def test_parse_since_variants():
    assert wake_actionable.parse_since(None) is None
    assert wake_actionable.parse_since("") is None
    assert wake_actionable.parse_since("garbage") is None
    naive = wake_actionable.parse_since("2026-07-14T12:00:00")
    assert naive == NOW
    # 带 Z / 偏移都能解析且归一为 naive
    assert wake_actionable.parse_since("2026-07-14T12:00:00Z").tzinfo is None
    assert wake_actionable.parse_since("2026-07-14T12:00:00+08:00").tzinfo is None


# ---- 判据分支 -------------------------------------------------------------
@pytest.mark.asyncio
async def test_empty_not_actionable():
    v = await _compute(FakeRepo())
    assert v["actionable"] is False
    assert v["busy_agents"] == 0
    assert v["live_runs"] == 0
    # 契约字段齐全
    for k in (
        "reasons", "terminal_runs_since", "finished_agents_since",
        "new_memos_since", "pending_briefings", "watermark", "project_id",
    ):
        assert k in v


@pytest.mark.asyncio
async def test_busy_agent_in_flight_not_actionable():
    repo = FakeRepo(agents=[_agent(AgentStatus.BUSY, "worker", NOW)])
    v = await _compute(repo)
    assert v["busy_agents"] == 1
    assert v["actionable"] is False  # busy = 在飞，不是 actionable


@pytest.mark.asyncio
async def test_finished_agent_after_since_is_actionable():
    repo = FakeRepo(agents=[_agent(AgentStatus.WAITING, "worker", AFTER)])
    v = await _compute(repo)
    assert v["finished_agents_since"] == 1
    assert v["actionable"] is True


@pytest.mark.asyncio
async def test_finished_agent_before_since_not_counted():
    repo = FakeRepo(agents=[_agent(AgentStatus.WAITING, "worker", BEFORE)])
    v = await _compute(repo)
    assert v["finished_agents_since"] == 0
    assert v["actionable"] is False


@pytest.mark.asyncio
async def test_live_run_not_actionable():
    repo = FakeRepo(runs=[_run("running", "sid")])
    v = await _compute(repo)
    assert v["live_runs"] == 1
    assert v["actionable"] is False


@pytest.mark.asyncio
async def test_terminal_run_after_since_is_actionable():
    repo = FakeRepo(runs=[_run("completed", "sid", AFTER)])
    v = await _compute(repo)
    assert v["terminal_runs_since"] == 1
    assert v["actionable"] is True


@pytest.mark.asyncio
async def test_terminal_run_session_mismatch_ignored():
    repo = FakeRepo(runs=[_run("killed", "other-session", AFTER)])
    v = await _compute(repo)
    assert v["terminal_runs_since"] == 0
    assert v["live_runs"] == 0
    assert v["actionable"] is False


@pytest.mark.asyncio
async def test_new_memos_is_actionable():
    repo = FakeRepo(memos_since=3)
    v = await _compute(repo)
    assert v["new_memos_since"] == 3
    assert v["actionable"] is True


@pytest.mark.asyncio
async def test_pending_briefings_do_not_trigger():
    repo = FakeRepo(briefings=2)
    v = await _compute(repo)
    assert v["pending_briefings"] == 2
    assert v["actionable"] is False  # briefings 仅展示，不触发唤醒


@pytest.mark.asyncio
async def test_never_throws_on_repo_error():
    class ExplodingRepo(FakeRepo):
        async def list_agents(self, team_id):
            raise RuntimeError("boom")

        async def list_workflow_runs(self, project_id="", limit=50):
            raise RuntimeError("boom")

    v = await _compute(ExplodingRepo())
    # 降级为保守值，绝不抛
    assert v["busy_agents"] == 0
    assert v["live_runs"] == 0
    assert v["actionable"] is False


# ---- 路由冒烟：真实 app + 空内存库，证明接线且不 500 -----------------------
def test_route_smoke_empty_db():
    import asyncio
    from contextlib import asynccontextmanager

    from fastapi.testclient import TestClient

    from aiteam.api import deps
    from aiteam.api.app import create_app
    from aiteam.storage.connection import close_db
    from aiteam.storage.repository import StorageRepository

    repo = StorageRepository(db_url="sqlite+aiosqlite://")
    asyncio.get_event_loop().run_until_complete(repo.init_db())
    deps._repository = repo

    app = create_app()

    @asynccontextmanager
    async def _noop_lifespan(app):
        yield

    app.router.lifespan_context = _noop_lifespan
    client = TestClient(app)
    try:
        resp = client.get("/api/wake/actionable", params={"session_id": "s1"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["actionable"] is False
        assert body["busy_agents"] == 0
        assert "watermark" in body
    finally:
        asyncio.get_event_loop().run_until_complete(close_db())
        deps._repository = None
