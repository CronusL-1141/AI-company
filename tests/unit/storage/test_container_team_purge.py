"""completed 会话容器队的定期清理 —— 判据与级联。

一个 CC 会话一支容器队、绝不跨会话复用（fleet 层 §3），所以一个长跑的 CC 进程
每换一次会话就在库里留下一具 completed 空壳。2026-07-27 生产库实测：80 支容器
队里 78 支已 completed，全部零任务、零会议、零 busy 成员——纯堆积。

这些用例钉死的是清理的**克制**。活队、非容器队、近期队、还有人在忙的队，以及
任何挂着实质记录（任务/会议/报告/调度/唤醒/工作流运行）的队，一支都不许删；
真删的时候必须把成员与成员活动一起带走，不留悬空行。
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import func, select

from aiteam.storage.connection import get_session
from aiteam.storage.models import AgentActivityModel, AgentModel, TeamModel

CONTAINER = {"kind": "session", "owner_session_id": "0def8f84-3b72-4b09-ae42-b13365ffeb65"}


async def _count(repo, model, **where) -> int:
    async with get_session(repo._db_url) as session:
        stmt = select(func.count()).select_from(model)
        for key, value in where.items():
            stmt = stmt.where(getattr(model, key) == value)
        return (await session.execute(stmt)).scalar_one()


async def _age(repo, team_id: str, days: float) -> None:
    """把队的三个时间戳一起推老。

    生产实测 13 支待清队的 ``completed_at`` **全是 NULL**（关队路径写 status 时
    没落时间戳），所以判龄必须能从 updated_at 兜底——这里刻意保持 completed_at
    为空，让用例走的正是生产的那条路径。
    """
    stamp = datetime.now() - timedelta(days=days)
    async with get_session(repo._db_url) as session:
        row = (
            await session.execute(select(TeamModel).where(TeamModel.id == team_id))
        ).scalar_one()
        row.created_at = stamp
        row.updated_at = stamp


async def _husk(repo, name: str = "session-0def8f84", *, days: float = 30):
    """一具典型空壳：completed 容器队 + 一个 offline 成员 + 若干成员活动。"""
    team = await repo.create_team(name=name, mode="coordinate", config=dict(CONTAINER))
    agent = await repo.create_agent(
        team_id=team.id, name="worker", role="worker", source="hook"
    )
    await repo.update_agent(agent.id, status="offline")
    await repo.create_activity(
        agent_id=agent.id, session_id="0def8f84", tool_name="Read"
    )
    await repo.update_team(team.id, status="completed")
    await _age(repo, team.id, days)
    return team, agent


@pytest.mark.asyncio
async def test_spent_husk_is_purged_with_its_roster(db_repository):
    team, agent = await _husk(db_repository)

    purged = await db_repository.purge_stale_session_containers(
        retention_days=7, limit=20
    )

    assert [p["team_id"] for p in purged] == [team.id]
    assert purged[0]["agents_deleted"] == 1
    assert purged[0]["activities_deleted"] == 1
    assert await db_repository.get_team(team.id) is None
    assert await _count(db_repository, AgentModel, team_id=team.id) == 0
    assert await _count(db_repository, AgentActivityModel, agent_id=agent.id) == 0


@pytest.mark.asyncio
async def test_live_container_is_never_touched(db_repository):
    """活着的容器队——本会话正在用的那支——绝不能被扫进来。"""
    team = await db_repository.create_team(
        name="session-80d0cc5e", mode="coordinate", config=dict(CONTAINER)
    )
    await _age(db_repository, team.id, 90)  # 老，但还 active

    assert await db_repository.purge_stale_session_containers(retention_days=7) == []
    assert await db_repository.get_team(team.id) is not None


@pytest.mark.asyncio
async def test_recent_husk_is_kept(db_repository):
    team, _ = await _husk(db_repository, days=3)

    assert await db_repository.purge_stale_session_containers(retention_days=7) == []
    assert await db_repository.get_team(team.id) is not None


@pytest.mark.asyncio
async def test_busy_member_blocks_purge(db_repository):
    """队 completed 却挂着 busy 成员是已知的矛盾态（reaper 另有复活逻辑）——
    这种队要留给复活路径处理，清理绝不能抢在前面把人连队一起删掉。"""
    team, agent = await _husk(db_repository)
    await db_repository.update_agent(agent.id, status="busy")

    assert await db_repository.purge_stale_session_containers(retention_days=7) == []
    assert await db_repository.get_team(team.id) is not None


@pytest.mark.asyncio
async def test_recently_active_member_blocks_purge(db_repository):
    """成员最近活跃是比队上任何时间戳都硬的"还在用"证据（2026-07-25 wenge 教训）。"""
    team, agent = await _husk(db_repository)
    await db_repository.update_agent(agent.id, last_active_at=datetime.now())

    assert await db_repository.purge_stale_session_containers(retention_days=7) == []
    assert await db_repository.get_team(team.id) is not None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "config"),
    [
        ("workflow-wf_374d39b3", {"kind": "workflow"}),
        ("session-legacy", {}),  # 无 kind 的历史遗留队
        ("ecosystem-platform", {"kind": "session"}),  # 有 kind 但不是容器命名
    ],
)
async def test_non_container_teams_are_never_touched(db_repository, name, config):
    """双判据：kind=session **且** 名字是 session-<sid8>，缺一不删。"""
    team = await db_repository.create_team(name=name, mode="coordinate", config=config)
    await db_repository.update_team(team.id, status="completed")
    await _age(db_repository, team.id, 90)

    assert await db_repository.purge_stale_session_containers(retention_days=7) == []
    assert await db_repository.get_team(team.id) is not None


@pytest.mark.asyncio
async def test_team_with_tasks_is_kept(db_repository):
    team, _ = await _husk(db_repository)
    await db_repository.create_task(team_id=team.id, title="还留着的活")

    assert await db_repository.purge_stale_session_containers(retention_days=7) == []
    assert await db_repository.get_team(team.id) is not None


@pytest.mark.asyncio
async def test_team_with_meetings_is_kept(db_repository):
    team, _ = await _husk(db_repository)
    await db_repository.create_meeting(team_id=team.id, topic="开过的会")

    assert await db_repository.purge_stale_session_containers(retention_days=7) == []
    assert await db_repository.get_team(team.id) is not None


@pytest.mark.asyncio
async def test_team_with_reports_is_kept(db_repository):
    team, _ = await _husk(db_repository)
    from aiteam.types import Report

    await db_repository.create_report(
        Report(author="worker", topic="留档", content="# 正文", team_id=team.id)
    )

    assert await db_repository.purge_stale_session_containers(retention_days=7) == []
    assert await db_repository.get_team(team.id) is not None


@pytest.mark.asyncio
async def test_limit_caps_one_cycle(db_repository):
    for i in range(5):
        await _husk(db_repository, name=f"session-husk{i}")

    purged = await db_repository.purge_stale_session_containers(
        retention_days=7, limit=2
    )

    assert len(purged) == 2
    assert await _count(db_repository, TeamModel) == 3


@pytest.mark.asyncio
async def test_non_positive_retention_disables_the_purge(db_repository):
    """关停开关必须真关停：0 天不等于"立刻全删"。"""
    team, _ = await _husk(db_repository)

    assert await db_repository.purge_stale_session_containers(retention_days=0) == []
    assert await db_repository.purge_stale_session_containers(retention_days=-1) == []
    assert await db_repository.get_team(team.id) is not None


class _RecordingBus:
    """验类型的桩——桩绝不能比生产宽松（批 9 教训：枚举缺项时单测绿、线上炸）。"""

    def __init__(self):
        self.events: list[tuple[str, str, dict]] = []

    async def emit(self, event_type, source, data, **_kwargs):
        from aiteam.types import EventType

        EventType(event_type)
        self.events.append((event_type, source, data))


@pytest.mark.asyncio
async def test_reaper_sweeps_and_leaves_a_trace(db_repository):
    from aiteam.api.state_reaper import StateReaper

    team, _ = await _husk(db_repository)
    bus = _RecordingBus()
    reaper = StateReaper(repo=db_repository, event_bus=bus)

    await reaper._purge_spent_session_containers(db_repository)

    assert await db_repository.get_team(team.id) is None
    ((event_type, source, data),) = bus.events
    assert event_type == "team.container_purged"
    assert source == f"team:{team.id}"
    assert data["name"] == "session-0def8f84"


@pytest.mark.asyncio
async def test_purge_rides_the_existing_reap_tick(db_repository, monkeypatch):
    """无定时器铁律：清理搭现有 reap 循环，不得新起任何调度。"""
    from aiteam.api.state_reaper import StateReaper

    reaper = StateReaper(repo=db_repository, event_bus=_RecordingBus())
    called: list[object] = []

    async def spy(repo=None):
        called.append(repo)

    async def noop(*_args, **_kwargs):
        return None

    monkeypatch.setattr(reaper, "_purge_spent_session_containers", spy)
    for heavy in (
        "_check_default_model_health",
        "_check_agent_liveness",
        "_backfill_agent_watermarks",
        "_check_scheduled_tasks",
        "_check_workflow_ingest",
    ):
        monkeypatch.setattr(reaper, heavy, noop)

    await reaper._reap_cycle_for_repo(db_repository)

    assert called == [db_repository]


@pytest.mark.asyncio
async def test_dry_run_reports_without_deleting(db_repository):
    team, agent = await _husk(db_repository)

    planned = await db_repository.purge_stale_session_containers(
        retention_days=7, dry_run=True
    )

    assert [p["team_id"] for p in planned] == [team.id]
    assert await db_repository.get_team(team.id) is not None
    assert await _count(db_repository, AgentModel, team_id=team.id) == 1
    assert await _count(db_repository, AgentActivityModel, agent_id=agent.id) == 1
