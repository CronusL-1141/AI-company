"""Q2 — 纯心跳不再落事件，而心跳**检测**必须分毫不变。

缔造者 2026-07-27 拍板停写，附硬条件：「停止心跳写入不是停止心跳检测吧？
别影响其他检测功能就可以」。这一条正是本文件要钉死的。

停写规模（生产库实测）：``agent.updated`` 共 112,349 行 = events 全表
237,126 行的 47.4%；其中 changes 恰好只有 ``last_active_at`` 的 94,029 行，
单这一类就占全表 39.7%（近 7 天 45,717 / 118,867 = 38.5%）。

停的是什么、没停什么：
- 停：``update_agent(last_active_at=…)`` 之后顺手再写的那一行事件。
- 没停：``agents.last_active_at`` 这一列照写。StateReaper 的超时判据、
  wake_actionable 的"刚收工"判据读的都是这一列，不是事件流。
"""

from __future__ import annotations

from datetime import timedelta

import pytest
import pytest_asyncio

from aiteam.api.event_bus import EventBus
from aiteam.api.state_reaper import StateReaper
from aiteam.clock import utc_now
from aiteam.storage.connection import close_db
from aiteam.storage.repository import StorageRepository


@pytest_asyncio.fixture()
async def repo():
    r = StorageRepository(db_url="sqlite+aiosqlite://")
    await r.init_db()
    yield r
    await close_db()


@pytest_asyncio.fixture()
async def agent(repo):
    team = await repo.create_team(name="t", mode="coordinate")
    a = await repo.create_agent(team_id=team.id, name="worker", role="worker", source="hook")
    yield await repo.get_agent(a.id)


async def _agent_events(repo, agent_id: str) -> list:
    return await repo.list_events(event_type="agent.updated", entity_id=agent_id, limit=100)


class TestWriteStop:
    @pytest.mark.asyncio
    async def test_pure_heartbeat_writes_the_column_but_no_event(self, repo, agent):
        beat = utc_now()
        await repo.update_agent(agent.id, last_active_at=beat)

        assert (await repo.get_agent(agent.id)).last_active_at == beat
        assert await _agent_events(repo, agent.id) == []

    @pytest.mark.asyncio
    async def test_repeated_heartbeats_stay_silent(self, repo, agent):
        for _ in range(50):
            await repo.update_agent(agent.id, last_active_at=utc_now())
        assert await _agent_events(repo, agent.id) == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "changes",
        [
            {"status": "offline"},
            {"current_task": "x"},
            {"session_id": "s-1"},
            # A heartbeat riding along with a real state change still counts.
            {"status": "busy", "last_active_at": utc_now()},
            # Context watermarks are deliberately still eventful.
            {"ctx_tokens": 100, "ctx_pct": 0.5},
        ],
    )
    async def test_any_real_change_still_emits(self, repo, agent, changes):
        await repo.update_agent(agent.id, **changes)
        events = await _agent_events(repo, agent.id)
        assert len(events) == 1
        assert set(events[0].data["changes"]) == set(changes)


class TestDetectionIsUnaffected:
    """The user's hard condition, expressed as behaviour rather than a promise."""

    @pytest.mark.asyncio
    async def test_fresh_silent_heartbeat_still_spares_the_agent(self, repo, agent):
        reaper = StateReaper(repo=repo, event_bus=EventBus(repo=repo))
        await repo.update_agent(agent.id, status="busy")
        await repo.update_agent(agent.id, last_active_at=utc_now())

        reaped = await reaper._check_hook_agent(
            await repo.get_agent(agent.id), utc_now(), repo
        )

        assert reaped is False
        assert (await repo.get_agent(agent.id)).status.value == "busy"

    @pytest.mark.asyncio
    async def test_stale_heartbeat_is_still_reaped(self, repo, agent):
        reaper = StateReaper(repo=repo, event_bus=EventBus(repo=repo))
        await repo.update_agent(agent.id, status="busy")
        await repo.update_agent(
            agent.id, last_active_at=utc_now() - timedelta(minutes=30)
        )

        reaped = await reaper._check_hook_agent(
            await repo.get_agent(agent.id), utc_now(), repo
        )

        assert reaped is True
        assert (await repo.get_agent(agent.id)).status.value == "offline"

    @pytest.mark.asyncio
    async def test_wake_actionable_still_sees_the_heartbeat(self, repo, agent):
        """`/api/wake/actionable` 的"刚从 busy 收工"判据也读这一列。"""
        from aiteam.api import wake_actionable

        beat = utc_now()
        await repo.update_agent(agent.id, status="waiting", last_active_at=beat)
        row = await repo.get_agent(agent.id)

        assert wake_actionable._after(row.last_active_at, beat - timedelta(minutes=1))
